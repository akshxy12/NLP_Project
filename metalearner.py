from torch import nn
from torch.nn import functional as F
from torch.utils.data import TensorDataset, DataLoader, RandomSampler
from torch.optim import Adam
from torch.nn import CrossEntropyLoss
from transformers import BertForSequenceClassification
from copy import deepcopy
import gc
from sklearn.metrics import accuracy_score
import torch
import numpy as np

class Learner(nn.Module):
    # Meta-learner
    def __init__(self, args):
        super(Learner, self).__init__()
        
        self.num_labels = args.num_labels
        self.outer_batch_size = args.outer_batch_size
        self.inner_batch_size = args.inner_batch_size
        self.outer_update_lr  = args.outer_update_lr
        self.inner_update_lr  = args.inner_update_lr
        self.inner_update_step = args.inner_update_step
        self.inner_update_step_eval = args.inner_update_step_eval
        self.bert_model = args.bert_model
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        self.model = BertForSequenceClassification.from_pretrained(self.bert_model, num_labels = self.num_labels)
        self.outer_optimizer = Adam(self.model.parameters(), lr=self.outer_update_lr)
        self.model.train()

    def forward(self, batch_tasks, training = True):
        task_accs = []
        sum_gradients = []
        num_task = len(batch_tasks)
        num_inner_update_step = self.inner_update_step if training else self.inner_update_step_eval

        for task_id, task in enumerate(batch_tasks):
            support = task[0]
            query   = task[1]
            
            fast_model = deepcopy(self.model)
            fast_model.to(self.device)
            support_dataloader = DataLoader(support, sampler=RandomSampler(support),
                                            batch_size=self.inner_batch_size)
            
            inner_optimizer = Adam(fast_model.parameters(), lr=self.inner_update_lr)
            fast_model.train()
            
            print('----Task',task_id, '----')
            for i in range(0,num_inner_update_step):
                all_loss = []
                for inner_step, batch in enumerate(support_dataloader):
                    
                    batch = tuple(t.to(self.device) for t in batch)
                    input_ids, attention_mask, segment_ids, label_id = batch
                    outputs = fast_model(input_ids, attention_mask, segment_ids, labels = label_id)
                    
                    loss = outputs[0]              
                    loss.backward()
                    inner_optimizer.step()
                    inner_optimizer.zero_grad()
                    
                    all_loss.append(loss.item())
                
                if i % 4 == 0:
                    print("Inner Loss: ", np.mean(all_loss))
            
            fast_model.to(torch.device('cpu'))
            
            if training:
                meta_weights = list(self.model.parameters())
                fast_weights = list(fast_model.parameters())

                gradients = []
                for i, (meta_params, fast_params) in enumerate(zip(meta_weights, fast_weights)):
                    gradient = meta_params - fast_params
                    if task_id == 0:
                        sum_gradients.append(gradient)
                    else:
                        sum_gradients[i] += gradient

            fast_model.to(self.device)
            fast_model.eval()
            with torch.no_grad():
                query_dataloader = DataLoader(query, sampler=None, batch_size=len(query))
                query_batch = iter(query_dataloader).next()
                query_batch = tuple(t.to(self.device) for t in query_batch)
                q_input_ids, q_attention_mask, q_segment_ids, q_label_id = query_batch
                q_outputs = fast_model(q_input_ids, q_attention_mask, q_segment_ids, labels = q_label_id)

                q_logits = F.softmax(q_outputs[1],dim=1)
                pre_label_id = torch.argmax(q_logits,dim=1)
                pre_label_id = pre_label_id.detach().cpu().numpy().tolist()
                q_label_id = q_label_id.detach().cpu().numpy().tolist()

                acc = accuracy_score(pre_label_id,q_label_id)
                task_accs.append(acc)
            
            fast_model.to(torch.device('cpu'))
            del fast_model, inner_optimizer
            torch.cuda.empty_cache()
        
        if training:
            for i in range(0,len(sum_gradients)):
                sum_gradients[i] = sum_gradients[i] / float(num_task)

            for i, params in enumerate(self.model.parameters()):
                params.grad = sum_gradients[i]

            self.outer_optimizer.step()
            self.outer_optimizer.zero_grad()
            
            del sum_gradients
            gc.collect()
        
        return np.mean(task_accs)
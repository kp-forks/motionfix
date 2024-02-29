# from torch.utils.data import Sampler
# import numpy as np

# class PercBatchSampler(Sampler):
#     def __init__(self, data_source,
#                  batch_size,
#                  dataset_percentages):
#         self.data_source = data_source
#         self.batch_size = batch_size
#         self.dataset_percentages = dataset_percentages
#         self.batches = self._precompute_batches()

#     def _group_indices_by_dataset(self):
#         # Group indices by dataset_name
#         dataset_indices = {}
#         for idx, item in enumerate(self.data_source):
#             dataset_name = item['dataset_name']
#             if dataset_name not in dataset_indices:
#                 dataset_indices[dataset_name] = []
#             dataset_indices[dataset_name].append(idx)
#         return dataset_indices

#     def _precompute_batches(self):
#         dataset_indices = self._group_indices_by_dataset()
#         samples_per_dataset = {name: int(round(self.batch_size * perc))
#                                for name, perc in self.dataset_percentages.items()}
#         batches = []
#         cur_avail_datasets = list(dataset_indices.keys())
#         remain_perc = 0
#         non_exist_ds = []
#         for k, v in samples_per_dataset.items():
#             if k not in cur_avail_datasets:
#                 remain_perc += v
#                 non_exist_ds.append(k)
#         for k in non_exist_ds:
#             del samples_per_dataset[k]
#         import math
#         share_for_others = math.floor(remain_perc / len(cur_avail_datasets))
#         for k in cur_avail_datasets:
#             samples_per_dataset[k] += share_for_others
#         cur_batch_sz = sum(list(samples_per_dataset.values()))
#         if cur_batch_sz < self.batch_size:
#             samples_per_dataset[cur_avail_datasets[0]] += self.batch_size - cur_batch_sz

#         # Calculate the minimum number of batches needed based on the dataset with the minimum samples
#         min_batches = min(len(indices) // count for count, indices in zip(samples_per_dataset.values(), dataset_indices.values()))
        
#         for _ in range(min_batches):
#             batch = []
#             for dataset_name, count in samples_per_dataset.items():
#                 choices = np.random.choice(dataset_indices[dataset_name], size=count, replace=False)
#                 batch.extend(choices)
#                 # Remove selected indices to avoid re-selection in future batches
#                 dataset_indices[dataset_name] = [idx for idx in dataset_indices[dataset_name] if idx not in choices]
#             batches.append(batch)
#         return batches

#     def __iter__(self):
#         # Shuffle batches to ensure different order for each epoch
#         np.random.shuffle(self.batches)
#         for batch in self.batches:
#             yield batch

#     def __len__(self):
#         return len(self.batches)

from torch.utils.data import Sampler
import numpy as np
import math
from itertools import cycle

class PercBatchSampler(Sampler):
    def __init__(self, data_source, batch_size, dataset_percentages):
        self.data_source = data_source
        self.batch_size = batch_size
        self.dataset_percentages = dataset_percentages
        self.batches = self._precompute_batches()

    def _group_indices_by_dataset(self):
        # Group indices by dataset_name
        dataset_indices = {}
        for idx, item in enumerate(self.data_source):
            dataset_name = item['dataset_name']
            if dataset_name not in dataset_indices:
                dataset_indices[dataset_name] = []
            dataset_indices[dataset_name].append(idx)
        return dataset_indices

    def _precompute_batches(self):
        dataset_indices = self._group_indices_by_dataset()
        samples_per_dataset = {
            name: int(round(self.batch_size * perc))
            for name, perc in self.dataset_percentages.items()
            if name in dataset_indices  # Ensure dataset exists
        }

        # Adjust samples per dataset if total is less than batch_size
        diff = self.batch_size - sum(samples_per_dataset.values())
        if diff > 0:
            samples_per_dataset[next(iter(samples_per_dataset))] += diff

        # Cycle through datasets if necessary
        dataset_cycles = {
            name: cycle(indices)
            for name, indices in dataset_indices.items()
        }

        total_items = sum(len(indices) for indices in dataset_indices.values())
        total_batches = math.ceil(total_items / self.batch_size)
        batches = []

        for _ in range(total_batches):
            batch = []
            for dataset_name, count in samples_per_dataset.items():
                batch.extend([next(dataset_cycles[dataset_name]) for _ in range(count)])
            batches.append(batch)
        return batches

    def __iter__(self):
        np.random.shuffle(self.batches)  # Shuffle batches for each epoch
        for batch in self.batches:
            yield batch

    def __len__(self):
        return len(self.batches)
import math
import random

import torch


class SequenceMask():
    def __init__(self, sequence_duration: int, mask_token_index: int):
        self.sequence_duration = sequence_duration
        self.mask_token_index = mask_token_index

    def sample_mask(self, batch_size: int = 1) -> torch.BoolTensor:
        raise NotImplementedError("subclass this")

    def apply_mask(self, input: torch.Tensor) -> torch.Tensor:
        mask = self.sample_mask(batch_size=input.shape[0])
        return input.masked_fill(mask, self.mask_token_index)


class BernoulliSequenceMask(SequenceMask):
    def __init__(self, probability: float, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.probability = probability

    def sample_mask(self, batch_size: int = 1) -> torch.BoolTensor:
        return (torch.ones(batch_size, self.sequence_duration)
                * self.probability
                ).bernoulli().bool()


class UniformProbabilityBernoulliSequenceMask(SequenceMask):
    def __init__(self, low: float = 0., high: float = 1.,
                 *args, **kwargs):
        assert 0 <= low < high <= 1
        super().__init__(*args, **kwargs)
        self.low = low
        self.high = high

    def sample_mask(self, batch_size: int = 1) -> torch.BoolTensor:
        # sample the Bernoulli masking probability
        masking_probability = random.uniform(self.low, self.high)
        return (torch.ones(batch_size, self.sequence_duration)
                * masking_probability
                ).bernoulli().bool()


class UniformMaskedAmountSequenceMask(SequenceMask):
    def __init__(self, min_masking_ratio: float = 0.,
                 *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.min_masking_ratio = min_masking_ratio
        self.min_masked_amount = math.ceil(
            self.sequence_duration * self.min_masking_ratio)

    def sample_mask(self, batch_size: int = 1) -> torch.BoolTensor:
        # sample the number of tokens to mask
        num_masked_tokens = torch.randint(self.min_masked_amount,
                                          self.sequence_duration + 1, (1,)
                                          ).item()

        # sample the indexes of tokens to mask
        mask_indexes_batched = torch.multinomial(
            torch.ones(batch_size, self.sequence_duration).float(),
            num_masked_tokens,
            replacement=False)

        # generate the mask
        num_tokens_total = batch_size * self.sequence_duration
        mask_sequence = torch.full((num_tokens_total,), False,
                                   dtype=torch.bool)
        mask_indexes_flattened = (
            mask_indexes_batched
            + (torch.arange(batch_size)*self.sequence_duration).unsqueeze(1)
        ).flatten()
        mask_sequence.index_fill_(0, mask_indexes_flattened, True)
        mask = mask_sequence.reshape(batch_size, self.sequence_duration)
        return mask


class ContiguousZonesSequenceMask(SequenceMask):
    def sample_mask(self, batch_size: int = 1) -> torch.BoolTensor:
        raise NotImplementedError("TODO")

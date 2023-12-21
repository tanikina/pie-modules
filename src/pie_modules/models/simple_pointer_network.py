import copy
import logging
from collections.abc import MutableMapping
from typing import Any, Dict, List, Optional

import torch
from pytorch_ie.core import PyTorchIEModel
from pytorch_lightning.utilities.types import OptimizerLRScheduler
from torchmetrics import Metric
from transformers import get_linear_schedule_with_warmup

from ..taskmodules.components.pointer_network import (
    PointerNetworkSpanAndRelationEncoderDecoder,
)
from .components.pointer_network.bart_as_pointer_network import BartAsPointerNetwork

logger = logging.getLogger(__name__)


STAGE_TRAIN = "train"
STAGE_VAL = "val"
STAGE_TEST = "test"


def _flatten_dict_gen(d, parent_key, sep):
    for k, v in d.items():
        new_key = parent_key + sep + k if parent_key else k
        if isinstance(v, MutableMapping):
            yield from flatten_dict(v, new_key, sep=sep).items()
        else:
            yield new_key, v


def flatten_dict(d: MutableMapping, parent_key: str = "", sep: str = "."):
    return dict(_flatten_dict_gen(d, parent_key, sep))


@PyTorchIEModel.register()
class SimplePointerNetworkModel(PyTorchIEModel):
    def __init__(
        self,
        model_name_or_path: str,
        target_token_ids: List[int],
        vocab_size: int,
        embedding_weight_mapping: Optional[Dict[int, List[int]]] = None,
        use_encoder_mlp: bool = False,
        annotation_encoder_decoder_name: str = "pointer_network_span_and_relation",
        annotation_encoder_decoder_kwargs: Optional[Dict[str, Any]] = None,
        metric_splits: List[str] = [STAGE_VAL, STAGE_TEST],
        metric_intervals: Optional[Dict[str, int]] = None,
        # optimizer / scheduler
        lr: float = 5e-5,
        layernorm_decay: float = 0.001,
        warmup_proportion: float = 0.0,
        # generation
        max_length: int = 512,
        num_beams: int = 4,
        override_generation_kwargs: Optional[Dict[str, Any]] = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,  # deprecated
        **kwargs,
    ):
        super().__init__(**kwargs)
        if generation_kwargs is not None:
            logger.warning(
                "generation_kwargs is deprecated and will be removed in a future version. "
                "Please use override_generation_kwargs instead."
            )
            override_generation_kwargs = generation_kwargs
        self.save_hyperparameters(ignore=["generation_kwargs"])

        self.lr = lr
        self.layernorm_decay = layernorm_decay
        self.warmup_proportion = warmup_proportion

        self.override_generation_kwargs = override_generation_kwargs or {}

        if annotation_encoder_decoder_name == "pointer_network_span_and_relation":
            self.annotation_encoder_decoder = PointerNetworkSpanAndRelationEncoderDecoder(
                **(annotation_encoder_decoder_kwargs or {}),
            )
        else:
            raise Exception(
                f"Unsupported annotation encoder decoder: {annotation_encoder_decoder_name}"
            )

        self.model = BartAsPointerNetwork.from_pretrained(
            model_name_or_path,
            # label id space (bos/eos/pad_token_id are also used for generation)
            bos_token_id=self.annotation_encoder_decoder.bos_id,
            eos_token_id=self.annotation_encoder_decoder.eos_id,
            pad_token_id=self.annotation_encoder_decoder.eos_id,
            label_ids=self.annotation_encoder_decoder.label_ids,
            # target token id space
            target_token_ids=target_token_ids,
            # mapping to better initialize the label embedding weights
            embedding_weight_mapping=embedding_weight_mapping,
            # other parameters
            use_encoder_mlp=use_encoder_mlp,
            # generation
            forced_bos_token_id=None,  # to disable ForcedBOSTokenLogitsProcessor
            forced_eos_token_id=None,  # to disable ForcedEOSTokenLogitsProcessor
            max_length=max_length,
            num_beams=num_beams,
        )

        self.model.resize_token_embeddings(vocab_size)

        if not self.is_from_pretrained:
            self.model.overwrite_decoder_label_embeddings_with_mapping()

        # NOTE: This is not a ModuleDict, so this will not live on the torch device!
        self.metrics: Dict[str, Metric] = {
            stage: self.annotation_encoder_decoder.get_metric() for stage in metric_splits
        }
        self.metric_intervals = metric_intervals or {}

    def predict(self, inputs, **kwargs) -> Dict[str, Any]:
        is_training = self.training
        self.eval()

        generation_kwargs = copy.deepcopy(self.annotation_encoder_decoder.generation_kwargs)
        generation_kwargs.update(self.override_generation_kwargs)
        generation_kwargs.update(kwargs)
        outputs = self.model.generate(inputs["src_tokens"], **generation_kwargs)

        if is_training:
            self.train()

        return {"pred": outputs}

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:
        inputs, _ = batch
        pred = self.predict(inputs=inputs)
        return pred

    def forward(self, inputs, **kwargs):
        input_ids = inputs["src_tokens"]
        attention_mask = inputs["src_attention_mask"]
        return self.model(input_ids=input_ids, attention_mask=attention_mask, **kwargs)

    def step(self, batch, stage: str, batch_idx: int) -> torch.FloatTensor:
        inputs, targets = batch
        if targets is None:
            raise ValueError("Targets must be provided for training or evaluation!")

        # Truncate the bos_id. The decoder input_ids will be created by the model
        # by shifting the labels one position to the right and adding the bos_id
        labels = targets["tgt_tokens"][:, 1:]
        decoder_attention_mask = targets["tgt_attention_mask"][:, 1:]

        outputs = self(inputs=inputs, labels=labels, decoder_attention_mask=decoder_attention_mask)
        loss = outputs.loss

        # show loss on each step only during training
        self.log(
            f"loss/{stage}", loss, on_step=(stage == STAGE_TRAIN), on_epoch=True, prog_bar=True
        )

        stage_metrics = self.metrics.get(stage, None)
        metric_interval = self.metric_intervals.get(stage, 1)
        if stage_metrics is not None and (batch_idx + 1) % metric_interval == 0:
            prediction = self.predict(inputs)
            stage_metrics.update(prediction["pred"], targets["tgt_tokens"])

        return loss

    def training_step(self, batch, batch_idx) -> torch.FloatTensor:
        loss = self.step(batch, stage=STAGE_TRAIN, batch_idx=batch_idx)

        return loss

    def validation_step(self, batch, batch_idx) -> torch.FloatTensor:
        loss = self.step(batch, stage=STAGE_VAL, batch_idx=batch_idx)

        return loss

    def test_step(self, batch, batch_idx) -> torch.FloatTensor:
        loss = self.step(batch, stage=STAGE_TEST, batch_idx=batch_idx)

        return loss

    def on_train_epoch_end(self) -> None:
        self._on_epoch_end(stage=STAGE_TRAIN)

    def on_validation_epoch_end(self) -> None:
        self._on_epoch_end(stage=STAGE_VAL)

    def on_test_epoch_end(self) -> None:
        self._on_epoch_end(stage=STAGE_TEST)

    def _on_epoch_end(self, stage: str) -> None:
        metrics = self.metrics.get(stage, None)
        if metrics is not None:
            metric_dict = metrics.compute()
            metrics.reset()
            metric_dict_flat = flatten_dict(d=metric_dict, sep="/")
            for k, v in metric_dict_flat.items():
                self.log(f"metric_{k}/{stage}", v, on_step=False, on_epoch=True, prog_bar=True)

    def configure_optimizers(self) -> OptimizerLRScheduler:
        parameters = []
        # head parameters
        params = {
            "lr": self.lr,
            "weight_decay": 1e-2,
            "params": dict(self.model.head_named_params()).values(),
        }
        parameters.append(params)

        # decoder only parameters
        params = {
            "lr": self.lr,
            "weight_decay": 1e-2,
            "params": dict(self.model.decoder_only_named_params()).values(),
        }
        parameters.append(params)

        # encoder only layernorm parameters
        params = {
            "lr": self.lr,
            "weight_decay": self.layernorm_decay,
            "params": [
                param
                for name, param in self.model.encoder_only_named_params()
                if ("layernorm" in name or "layer_norm" in name)
            ],
        }
        parameters.append(params)

        # encoder only other parameters
        params = {
            "lr": self.lr,
            "weight_decay": 1e-2,
            "params": [
                param
                for name, param in self.model.encoder_only_named_params()
                if not ("layernorm" in name or "layer_norm" in name)
            ],
        }
        parameters.append(params)

        # encoder-decoder shared parameters
        params = {
            "lr": self.lr,
            "weight_decay": 1e-2,
            "params": dict(self.model.encoder_decoder_shared_named_params()).values(),
        }
        parameters.append(params)

        optimizer = torch.optim.AdamW(parameters)

        if self.warmup_proportion > 0.0:
            stepping_batches = self.trainer.estimated_stepping_batches
            scheduler = get_linear_schedule_with_warmup(
                optimizer, int(stepping_batches * self.warmup_proportion), stepping_batches
            )
            return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
        else:
            return optimizer

import copy
import logging
from collections.abc import MutableMapping
from typing import Any, Dict, List, Optional, Set, Type, Union

import torch
from pytorch_ie.auto import AutoTaskModule
from pytorch_ie.core import PyTorchIEModel
from pytorch_lightning.utilities.types import OptimizerLRScheduler
from torch.optim import Optimizer
from torchmetrics import Metric
from transformers import PreTrainedModel, get_linear_schedule_with_warmup

from pie_modules.taskmodules.common import HasConfigureMetric
from pie_modules.utils import resolve_type

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
        base_model_config: Dict[str, Any],
        # TODO: do not provide a default value here
        base_model_type: Union[
            str, Type[PreTrainedModel]
        ] = "pie_modules.models.base_models.BartAsPointerNetwork",
        taskmodule_config: Optional[Dict[str, Any]] = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
        # metrics
        metric_splits: List[str] = [STAGE_VAL, STAGE_TEST],
        metric_intervals: Optional[Dict[str, int]] = None,
        use_prediction_for_metrics: Union[bool, List[str]] = True,
        # scheduler / optimizer
        warmup_proportion: float = 0.0,
        # important: this is only used if the base model does not have a configure_optimizer method
        learning_rate: Optional[float] = None,
        optimizer_type: Optional[Union[str, Type[Optimizer]]] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.save_hyperparameters()

        # optimizer / scheduler
        self.learning_rate = learning_rate
        self.optimizer_type = optimizer_type
        self.warmup_proportion = warmup_proportion

        # can be used to override the default generation setup (created from the base model generation config)
        self.generation_kwargs = generation_kwargs or {}

        resolved_base_model_type = resolve_type(
            base_model_type, expected_super_type=PreTrainedModel
        )
        self.model = resolved_base_model_type.from_pretrained(**base_model_config)

        self.use_prediction_for_metrics: Set[str]
        if isinstance(use_prediction_for_metrics, bool):
            self.use_prediction_for_metrics = (
                set(metric_splits) if use_prediction_for_metrics else set()
            )
        else:
            self.use_prediction_for_metrics = set(use_prediction_for_metrics)
        missed_stages = self.use_prediction_for_metrics - set(metric_splits)
        if len(missed_stages) > 0:
            raise ValueError(
                f"There are stages in use_prediction_for_metrics that are not in metric_splits: "
                f"{missed_stages}. Available metric splits: {metric_splits}."
            )

        self.metric_intervals = metric_intervals or {}
        self.metrics: Dict[str, Metric] = {}
        if taskmodule_config is not None:
            # TODO: use AutoTaskModule.from_config() when it is available
            taskmodule = AutoTaskModule._from_pretrained(
                model_id="",
                revision=None,
                cache_dir=None,
                force_download=False,
                proxies=None,
                resume_download=False,
                local_files_only=False,
                token=None,
                map_location="cpu",
                strict=False,
                config=taskmodule_config,
            )
            # TODO: remove this check when TaskModule.build_metric() is implemented
            if not isinstance(taskmodule, HasConfigureMetric):
                raise Exception(
                    f"taskmodule {taskmodule} does not implement HasConfigureMetric interface"
                )
            # NOTE: This is not a ModuleDict, so this will not live on the torch device!
            self.metrics = {stage: taskmodule.configure_metric(stage) for stage in metric_splits}

    def predict(self, inputs, **kwargs) -> torch.LongTensor:
        is_training = self.training
        self.eval()

        generation_kwargs = copy.deepcopy(self.generation_kwargs)
        generation_kwargs.update(kwargs)
        outputs = self.model.generate(inputs["input_ids"], **generation_kwargs)

        if is_training:
            self.train()

        # TODO: move into base model? or does this work for "all" generative models?
        # strip the bos_id
        if isinstance(outputs, torch.Tensor):
            return outputs[:, 1:]
        else:
            raise ValueError(f"Unsupported output type: {type(outputs)}")

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:
        inputs, _ = batch
        pred = self.predict(inputs=inputs)
        return pred

    def forward(self, inputs, **kwargs):
        input_ids = inputs["input_ids"]
        attention_mask = inputs["attention_mask"]
        return self.model(input_ids=input_ids, attention_mask=attention_mask, **kwargs)

    def step(self, batch, stage: str, batch_idx: int) -> torch.FloatTensor:
        inputs, targets = batch
        if targets is None:
            raise ValueError("Targets must be provided for training or evaluation!")

        labels = targets["labels"]
        decoder_attention_mask = targets["decoder_attention_mask"]

        outputs = self(inputs=inputs, labels=labels, decoder_attention_mask=decoder_attention_mask)
        loss = outputs.loss

        # show loss on each step only during training
        self.log(
            f"loss/{stage}", loss, on_step=(stage == STAGE_TRAIN), on_epoch=True, prog_bar=True
        )

        stage_metrics = self.metrics.get(stage, None)
        metric_interval = self.metric_intervals.get(stage, 1)
        if stage_metrics is not None and (batch_idx + 1) % metric_interval == 0:
            if stage in self.use_prediction_for_metrics:
                prediction = self.predict(inputs)
            else:
                # construct prediction from the model output
                logits = outputs.logits
                # get the indices (these are without the initial bos_ids, see above)
                prediction = torch.argmax(logits, dim=-1)
            # the format of expected needs to be the same as the format of prediction
            stage_metrics.update(prediction, labels)

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
        if self.metrics is not None:
            metrics = self.metrics.get(stage, None)
            if metrics is not None:
                metric_dict = metrics.compute()
                metrics.reset()
                # TODO: consider https://lightning.ai/docs/torchmetrics/stable/pages/overview.html#metriccollection
                #  and self.log_dict()
                metric_dict_flat = flatten_dict(d=metric_dict, sep="/")
                for k, v in metric_dict_flat.items():
                    self.log(f"metric_{k}/{stage}", v, on_step=False, on_epoch=True, prog_bar=True)

    def configure_optimizers(self) -> OptimizerLRScheduler:
        if hasattr(self.model, "configure_optimizer") and callable(self.model.configure_optimizer):
            if self.learning_rate is not None:
                raise ValueError(
                    f"learning_rate is set to {self.learning_rate}, but the *base model* ({type(self.model)}) has a "
                    f"configure_optimizer method. Please set learning_rate to None and configure the optimizer "
                    f"inside the *base model*."
                )
            optimizer = self.model.configure_optimizer()
        else:
            logger.warning(
                f"The model does not have a configure_optimizer method. Creating an optimizer of "
                f"optimizer_type={self.optimizer_type} with the learning_rate={self.learning_rate} instead."
            )
            if self.optimizer_type is None:
                raise ValueError(
                    f"optimizer_type is None, but the *base model* ({type(self.model)}) does not have a "
                    f"configure_optimizer method. Please set the optimizer_type to a valid optimizer type, "
                    f"e.g. optimizer_type=torch.optim.Adam."
                )
            resolved_optimizer_type = resolve_type(
                self.optimizer_type, expected_super_type=Optimizer
            )
            optimizer = resolved_optimizer_type(self.parameters(), lr=self.learning_rate)

        if self.warmup_proportion > 0.0:
            stepping_batches = self.trainer.estimated_stepping_batches
            scheduler = get_linear_schedule_with_warmup(
                optimizer, int(stepping_batches * self.warmup_proportion), stepping_batches
            )
            return [optimizer], [{"scheduler": scheduler, "interval": "step"}]
        else:
            return optimizer

#!/usr/bin/env python3
# *_* coding: utf-8 *_*

# # Copyright 2022 Ant Group Co., Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""sl model base
"""
import copy
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
import tensorflow as tf
import torch
from tensorflow.python.keras import callbacks as callbacks_module
from torch import nn
from torch.nn.modules.loss import _Loss as BaseTorchLoss
from torch.optim import Optimizer
from torch.utils.data import DataLoader

import secretflow.device as ft

from secretflow.device import PYUObject, proxy
from secretflow.security.privacy import DPStrategy
from secretflow.utils.compressor import Compressor, SparseCompressor


class SLBaseModel(ABC):
    def __init__(self, builder_base: Callable, builder_fuse: Callable = None):
        self.model_base = builder_base() if builder_base is not None else None
        self.model_fuse = builder_fuse() if builder_fuse is not None else None

    @abstractmethod
    def build_dataset(
        self,
        x: np.ndarray,
        y: Optional[np.ndarray] = None,
        s_w: Optional[np.ndarray] = None,
        batch_size=32,
        buffer_size=128,
        repeat_count=1,
    ):
        pass

    @abstractmethod
    def base_forward(self):
        pass

    @abstractmethod
    def base_backward(self):
        pass

    @abstractmethod
    def fuse_net(self, hiddens):
        pass


class SLBaseModule(ABC, nn.Module):
    @abstractmethod
    def forward(self, x):
        pass

    def get_weights(self):
        return {k: v.cpu() for k, v in self.state_dict().items()}

    def set_weights(self, weights):
        self.load_state_dict(weights)

    def get_gradients(self, parameters=None):
        if parameters is None:
            parameters = self.parameters()
        grads = []
        for p in parameters:
            grad = None if p.grad is None else p.grad.data.cpu().numpy()
            grads.append(grad)
        return grads

    def set_gradients(
        self,
        gradients: List[Union[torch.Tensor, np.ndarray]],
        parameters: Optional[List[torch.Tensor]] = None,
    ):
        if parameters is None:
            parameters = self.parameters()
        for g, p in zip(gradients, parameters):
            if g is not None:
                p.grad = torch.from_numpy(g.copy())


class SLBaseTFModel(SLBaseModel):
    def __init__(
        self,
        builder_base: Callable[[], tf.keras.Model],
        builder_fuse: Callable[[], tf.keras.Model],
        dp_strategy: DPStrategy,
        compressor: Compressor,
        **kwargs,
    ):
        super().__init__(builder_base, builder_fuse)

        self.dp_strategy = dp_strategy
        self.embedding_dp = (
            self.dp_strategy.embedding_dp if dp_strategy is not None else None
        )
        self.label_dp = self.dp_strategy.label_dp if dp_strategy is not None else None
        self.compressor = compressor

        self.train_set = None
        self.eval_set = None
        self.valid_set = None
        self.tape = None
        self.h = None
        self.train_x, self.train_y = None, None
        self.eval_x, self.eval_y = None, None
        self.kwargs = {}
        self.has_y = False
        self.has_s_w = False
        self.train_sample_weight = None
        self.eval_sample_weight = None
        self.fuse_callbacks = None
        self.logs = None
        self.epoch_logs = None
        self.training_logs = None
        self.steps_per_epoch = None

    @staticmethod
    @tf.custom_gradient
    def fuse_op(x, y):
        def grad(upstream):
            return upstream * y, upstream * y

        return x, grad

    def init_data(self):
        self.train_x, self.train_y = None, None
        self.eval_x, self.eval_y = None, None
        self.train_sample_weight = None
        self.eval_sample_weight = None

    def set_steps_per_epoch(self, steps_per_epoch):
        self.steps_per_epoch = steps_per_epoch

    def get_basenet_output_num(self):
        return len(self.model_base.outputs)

    def build_dataset(
        self,
        *x: List[np.ndarray],
        y: Optional[np.ndarray] = None,
        s_w: Optional[np.ndarray] = None,
        batch_size=32,
        buffer_size=128,
        shuffle=False,
        repeat_count=1,
        stage="train",
        random_seed=1234,
        dataset_builder: Callable = None,
    ):
        """build tf.data.Dataset

        Args:
            x: feature, FedNdArray or HDataFrame
            y: label, FedNdArray or HDataFrame
            s_w: sample weight, FedNdArray or HDataFrame
            batch_size: Number of samples per gradient update
            buffer_size: buffer size for shuffling
            shuffle: whether shuffle the dataset or not
            repeat_count: num of repeats
            stage: stage of this datset
            random_seed: Prg seed for shuffling
        """
        assert len(x) > 0 or x[0] is not None, "X can not be None, please check"
        x = [xi for xi in x]
        self.has_y = False
        self.has_s_w = False
        if y is not None and len(y.shape) > 0:
            self.has_y = True
            x.append(y)
            if s_w is not None and len(s_w.shape) > 0:
                self.has_s_w = True
                x.append(s_w)

        steps_per_epoch = None
        if dataset_builder is None:
            # convert pandas.DataFrame to numpy.ndarray
            x = [t.values if isinstance(t, pd.DataFrame) else t for t in x]
            # https://github.com/tensorflow/tensorflow/issues/20481
            x = x[0] if len(x) == 1 else tuple(x)

            data_set = (
                tf.data.Dataset.from_tensor_slices(x)
                .batch(batch_size)
                .repeat(repeat_count)
            )
            if shuffle:
                data_set = data_set.shuffle(buffer_size, seed=random_seed)
        else:
            data_set = dataset_builder(x)
            steps_per_epoch = data_set.steps_per_epoch
            self.steps_per_epoch = steps_per_epoch

        data_set = iter(data_set)

        if stage == "train":
            self.train_set = data_set
        elif stage == "eval":
            self.eval_set = data_set
        else:
            raise Exception(f"Illegal argument stage={stage}")
        return steps_per_epoch

    @tf.function
    def _base_forward_internal(self, data_x, h):
        h = self.model_base(data_x)

        # Embedding differential privacy
        if self.embedding_dp is not None:
            if isinstance(h, List):
                h = [self.embedding_dp(hi) for hi in h]
            else:
                h = self.embedding_dp(h)
        return h

    def base_forward(self, stage="train", compress: bool = False):
        """compute hidden embedding
        Args:
            stage: Which stage of the base forward
            compress: Whether to compress cross device data.
        Returns: hidden embedding
        """

        assert (
            self.model_base is not None
        ), "Base model cannot be none, please give model define or load a trained model"

        data_x = None
        self.init_data()
        if stage == "train":
            train_data = next(self.train_set)
            if self.has_y:
                if self.has_s_w:
                    data_x = train_data[:-2]
                    train_y = train_data[-2]
                    self.train_sample_weight = train_data[-1]
                else:
                    data_x = train_data[:-1]
                    train_y = train_data[-1]
                # Label differential privacy
                if self.label_dp is not None:
                    dp_train_y = self.label_dp(train_y.numpy())
                    self.train_y = tf.convert_to_tensor(dp_train_y)
                else:
                    self.train_y = train_y
            else:
                data_x = train_data
        elif stage == "eval":
            eval_data = next(self.eval_set)
            if self.has_y:
                if self.has_s_w:
                    data_x = eval_data[:-2]
                    eval_y = eval_data[-2]
                    self.eval_sample_weight = eval_data[-1]
                else:
                    data_x = eval_data[:-1]
                    eval_y = eval_data[-1]
                # Label differential privacy
                if self.label_dp is not None:
                    dp_eval_y = self.label_dp(eval_y.numpy())
                    self.eval_y = tf.convert_to_tensor(dp_eval_y)
                else:
                    self.eval_y = eval_y
            else:
                data_x = eval_data
        else:
            raise Exception("invalid stage")

        # Strip tuple of length one, e.g: (x,) -> x
        data_x = data_x[0] if isinstance(data_x, Tuple) and len(data_x) == 1 else data_x

        self.tape = tf.GradientTape(persistent=True)
        with self.tape:
            self.h = self._base_forward_internal(
                data_x,
                self.h,
            )
        if compress:
            if self.compressor:
                return self.compressor.compress(self.h.numpy())
            else:
                raise Exception(
                    'can not find compressor when compress data in base_forward'
                )
        return self.h

    @tf.function
    def _base_backword_internal(self, gradients, trainable_vars):

        self.model_base.optimizer.apply_gradients(zip(gradients, trainable_vars))

    def base_backward(self, gradient, compress: bool = False):
        """backward on fusenet

        Args:
            gradient: gradient of fusenet hidden layer
            compress: Whether to decompress gradient.
        """

        return_hiddens = []

        if compress:
            if self.compressor:
                gradient = self.compressor.decompress(gradient)
            else:
                raise Exception(
                    'can not find compressor when decompress data in base_backward'
                )
        with self.tape:
            if len(gradient) == len(self.h):
                for i in range(len(gradient)):
                    return_hiddens.append(self.fuse_op(self.h[i], gradient[i]))
            else:
                gradient = gradient[0]
                return_hiddens.append(self.fuse_op(self.h, gradient))

        trainable_vars = self.model_base.trainable_variables
        gradients = self.tape.gradient(return_hiddens, trainable_vars)

        self._base_backword_internal(gradients, trainable_vars)

        # clear intermediate results
        self.tape = None
        self.h = None
        self.kwargs = {}

    def get_base_weights(self):
        return self.model_base.get_weights()

    def get_fuse_weights(self):
        return self.model_fuse.get_weights() if self.model_fuse is not None else None

    def init_training(self, callbacks, epochs=1, steps=0, verbose=0):
        if not isinstance(callbacks, callbacks_module.CallbackList):
            self.fuse_callbacks = callbacks_module.CallbackList(
                callbacks,
                add_history=True,
                add_progbar=verbose != 0,
                model=self.model_fuse,
                verbose=verbose,
                epochs=epochs,
                steps=steps,
                steps_per_epoch=self.steps_per_epoch,
            )
        else:
            raise NotImplementedError

    def get_stop_training(self):
        return self.model_fuse.stop_training

    def on_train_begin(self):
        self.fuse_callbacks.on_train_begin()

    def on_epoch_begin(self, epoch):
        self.fuse_callbacks.on_epoch_begin(epoch)

    def on_train_batch_begin(self, step=None):
        assert step is not None, "Step cannot be none"
        self.fuse_callbacks.on_train_batch_begin(step)

    def on_train_batch_end(self, step=None):
        assert step is not None, "Step cannot be none"
        self.epoch_logs = copy.deepcopy(self.logs)
        self.fuse_callbacks.on_train_batch_end(step, self.logs)

    def on_validation(self, val_logs):
        val_logs = {'val_' + name: val for name, val in val_logs.items()}
        self.epoch_logs.update(val_logs)

    def on_epoch_end(self, epoch):

        self.fuse_callbacks.on_epoch_end(epoch, self.epoch_logs)
        self.training_logs = self.epoch_logs
        return self.epoch_logs

    def on_train_end(self):
        self.fuse_callbacks.on_train_end(logs=self.training_logs)
        return self.model_fuse.history.history

    def set_sample_weight(self, sample_weight, stage="train"):
        if stage == "train":
            self.train_sample_weight = sample_weight
        elif stage == "eval":
            self.eval_sample_weight = sample_weight
        else:
            raise Exception("Illegal Argument")

    def fuse_net(self, *hidden_features, _num_returns=2, compress=False):
        """Fuses the hidden layer and calculates the reverse gradient
        only on the side with the label

        Args:
            hidden_features: A list of hidden layers for each party to compute
            compress: Whether to decompress/compress data.
        Returns:
            gradient Of hiddens
        """
        assert (
            self.model_fuse is not None
        ), "Fuse model cannot be none, please give model define"

        if compress:
            if self.compressor:
                # save fuse_sparse_masks to apply on gradients
                if isinstance(self.compressor, SparseCompressor):
                    fuse_sparse_masks = list(
                        map(lambda d: (d != 0) * 1, hidden_features)
                    )
                hidden_features = self.compressor.decompress(hidden_features)
            else:
                raise Exception(
                    'can not find compressor when decompress data in fuse_net'
                )

        hiddens = []
        for h in hidden_features:
            # h will be list, if basenet is multi output
            if isinstance(h, List):
                for i in range(len(h)):
                    hiddens.append(tf.convert_to_tensor(h[i]))
            else:
                hiddens.append(tf.convert_to_tensor(h))

        logs = {}

        gradient = self._fuse_net_internal(
            hiddens,
            self.train_y,
            self.train_sample_weight,
        )

        for m in self.model_fuse.metrics:
            logs['train_' + m.name] = m.result().numpy()
        self.logs = logs
        if compress:
            gradient = [g.numpy() for g in gradient]
            gradient = self.compressor.compress(gradient)
            # apply fuse_sparse_masks on gradients
            if fuse_sparse_masks:
                assert len(fuse_sparse_masks) == len(
                    gradient
                ), f'length of fuse_sparse_masks and gradient mismatch: {len(fuse_sparse_masks)} - {len(gradient)}'
                gradient = list(
                    map(lambda d, m: d.multiply(m), gradient, fuse_sparse_masks)
                )
        return gradient

    @tf.function
    def _fuse_net_internal(self, hiddens, train_y, train_sample_weight):
        with tf.GradientTape(persistent=True) as tape:
            for h in hiddens:
                tape.watch(h)

            # Step 1: forward pass
            y_pred = self.model_fuse(hiddens, training=True, **self.kwargs)

            # Step 2: loss calculation, the loss function is configured in `compile()`.
            # if isinstance(self.model_fuse.loss, tfutils.custom_loss):
            #     self.model_fuse.loss.with_kwargs(kwargs)
            loss = self.model_fuse.compiled_loss(
                train_y,
                y_pred,
                sample_weight=train_sample_weight,
                regularization_losses=self.model_fuse.losses,
            )

        # Step3: compute gradients
        trainable_vars = self.model_fuse.trainable_variables
        gradients = tape.gradient(loss, trainable_vars)
        self.model_fuse.optimizer.apply_gradients(zip(gradients, trainable_vars))

        # Step4: update metrics
        self.model_fuse.compiled_metrics.update_state(
            train_y, y_pred, sample_weight=train_sample_weight
        )

        return tape.gradient(loss, hiddens)

    def reset_metrics(self):
        self.model_fuse.compiled_metrics.reset_state()
        self.model_fuse.compiled_loss.reset_state()

    @tf.function
    def _evaluate_internal(self, hiddens, eval_y, eval_sample_weight):
        # Step 1: forward pass
        y_pred = self.model_fuse(hiddens, training=False, **self.kwargs)

        # Step 2: update loss
        # custom loss will be re-open in the next version
        # if isinstance(self.model_fuse.loss, tfutils.custom_loss):
        #     self.model_fuse.loss.with_kwargs(kwargs)
        self.model_fuse.compiled_loss(
            eval_y,
            y_pred,
            sample_weight=eval_sample_weight,
            regularization_losses=self.model_fuse.losses,
        )
        # Step 3: update metrics
        self.model_fuse.compiled_metrics.update_state(
            eval_y, y_pred, sample_weight=eval_sample_weight
        )

        result = {}
        for m in self.model_fuse.metrics:
            result[m.name] = m.result()
        return result

    def evaluate(self, *hidden_features, compress: bool = False):
        """Returns the loss value & metrics values for the model in test mode.

        Args:
            hidden_features: A list of hidden layers for each party to compute
            compress: Whether to decompress input data.
        Returns:
            map of model metrics.
        """

        assert (
            self.model_fuse is not None
        ), "model cannot be none, please give model define"
        if compress:
            if self.compressor:
                hidden_features = self.compressor.decompress(hidden_features)
            else:
                raise Exception(
                    'can not find compressor when decompress data in evaluate'
                )
        hiddens = []
        for h in hidden_features:
            if isinstance(h, List):
                for i in range(len(h)):
                    hiddens.append(tf.convert_to_tensor(h[i]))
            else:
                hiddens.append(tf.convert_to_tensor(h))
        metrics = self._evaluate_internal(
            hiddens=hiddens,
            eval_y=self.eval_y,
            eval_sample_weight=self.eval_sample_weight,
        )
        result = {}
        for k, v in metrics.items():
            result[k] = v.numpy()
        return result

    def metrics(self):
        return self.model_fuse.metrics

    @tf.function
    def _predict_internal(self, hiddens):
        y_pred = self.model_fuse(hiddens)
        return y_pred

    def predict(self, *hidden_features, compress: bool = False):
        """Generates output predictions for the input hidden layer features.

        Args:
            hidden_features: A list of hidden layers for each party to compute
            compress: Whether to decompress input data.
        Returns:
            Array(s) of predictions.
        """
        assert (
            self.model_fuse is not None
        ), "Fuse model cannot be none, please give model define"
        if compress:
            if self.compressor:
                hidden_features = self.compressor.decompress(hidden_features)
            else:
                raise Exception(
                    'can not find compressor when decompress data in predict'
                )

        hiddens = []
        for h in hidden_features:
            if isinstance(h, List):
                for i in range(len(h)):
                    hiddens.append(tf.convert_to_tensor(h[i]))
            else:
                hiddens.append(tf.convert_to_tensor(h))
        y_pred = self._predict_internal(hiddens)
        return y_pred

    def save_base_model(self, base_model_path: str, save_traces=True):
        Path(base_model_path).parent.mkdir(parents=True, exist_ok=True)
        assert base_model_path is not None, "model path cannot be empty"
        self.model_base.save(base_model_path, save_traces=save_traces)

    def save_fuse_model(self, fuse_model_path: str, save_traces=True):
        Path(fuse_model_path).parent.mkdir(parents=True, exist_ok=True)
        assert fuse_model_path is not None, "model path cannot be empty"
        self.model_fuse.save(fuse_model_path, save_traces=save_traces)

    def load_base_model(self, base_model_path: str, custom_objects=None):
        self.init_data()
        assert base_model_path is not None, "model path cannot be empty"
        self.model_base = tf.keras.models.load_model(
            base_model_path, custom_objects=custom_objects
        )

    def load_fuse_model(self, fuse_model_path: str, custom_objects=None):
        assert fuse_model_path is not None, "model path cannot be empty"
        self.model_fuse = tf.keras.models.load_model(
            fuse_model_path, custom_objects=custom_objects
        )

    def get_privacy_spent(self, step: int, orders=None):
        """Get accountant of dp mechanism.

        Args:
            step: The current step of model training or prediction.
            orders: An array (or a scalar) of RDP orders.
        """
        privacy_dict = self.dp_strategy.get_privacy_spent(step, orders)
        return privacy_dict


class ModelPartition(object):
    def __init__(self, model_fn, optim_fn, loss_fn, dataloader_fn):
        self.model: SLBaseModule = model_fn()
        self.optimizer: Optimizer = optim_fn(self.model.parameters())
        self.loss: BaseTorchLoss = loss_fn()
        self._dataloader: Dict[str, DataLoader] = {
            k: dl_fn() for k, dl_fn in dataloader_fn.items()
        }
        self._dataiter: Dict[str, Iterator] = {
            k: iter(_dl) for k, _dl in self._dataloader.items()
        }

    def get_one_batch(self, name='train'):
        try:
            x, y = next(self._dataiter[name])
        except StopIteration:
            self._dataiter = iter(self._dataloader[name])
            x, y = next(self._dataiter)
        return x, y

    def forward(
        self, used_name='train', external_input=None
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if external_input is None:
            external_input = {}
        x, y = self.get_one_batch(used_name)
        y_pred = self.model(x, **external_input)
        return y_pred, y

    def zero_grad(self):
        self.optimizer.zero_grad()

    def backward(self, used_name='train', gradients=None, external_input: Dict = None):
        if gradients is not None:
            self.model.set_gradients(gradients)
        else:
            if external_input is None:
                external_input = {}
            y_pred, y = self.forward(used_name, external_input)
            loss = self.loss(y_pred, y)
            loss.backward()
        return self.model.get_gradients()

    def apply_gradients(self, gradients=None):
        if gradients is not None:
            self.model.set_gradients(gradients)
        self.optim_step()

    def optim_step(self):
        self.optimizer.step()
        return self.model.get_weights()

    def get_weights(self):
        return self.model.get_weights()

    def set_weights(self, weights):
        self.model.set_weights(weights)

    def call_model_fn(self, fn_name, *args, **kwargs):
        # TODO: a temporary utils
        return getattr(self.model, fn_name)(*args, **kwargs)


@proxy(PYUObject)
class PYUSLTFModel(SLBaseTFModel):
    pass


@proxy(ft.PYUObject)
class PYUModel(ModelPartition):
    pass

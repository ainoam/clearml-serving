import os
from typing import Optional, Any, Callable, List

import numpy as np
from clearml import Task, Model
from clearml.binding.artifacts import Artifacts
from clearml.storage.util import sha256sum
from requests import post as request_post

from .endpoints import ModelEndpoint


class BasePreprocessRequest(object):
    __preprocessing_lookup = {}
    __preprocessing_modules = set()
    _default_serving_base_url = "http://127.0.0.1:8080/serve/"
    _server_config = {}  # externally configured by the serving inference service
    _timeout = None  # timeout in seconds for the entire request, set in __init__

    def __init__(
            self,
            model_endpoint: ModelEndpoint,
            task: Task = None,
    ):
        """
        Notice this object is not be created per request, but once per Process
        Make sure it is always thread-safe
        """
        self.model_endpoint = model_endpoint
        self._preprocess = None
        self._model = None
        if self._timeout is None:
            self._timeout = int(float(os.environ.get('GUNICORN_SERVING_TIMEOUT', 600)) * 0.8)

        # load preprocessing code here
        if self.model_endpoint.preprocess_artifact:
            if not task or self.model_endpoint.preprocess_artifact not in task.artifacts:
                raise ValueError("Error: could not find preprocessing artifact \'{}\' on Task id={}".format(
                    self.model_endpoint.preprocess_artifact, task.id))
            else:
                try:
                    self._instantiate_custom_preprocess_cls(task)
                except Exception as ex:
                    raise ValueError("Error: Failed loading preprocess code for \'{}\': {}".format(
                        self.model_endpoint.preprocess_artifact, ex))

    def _instantiate_custom_preprocess_cls(self, task: Task) -> None:
        path = task.artifacts[self.model_endpoint.preprocess_artifact].get_local_copy()
        # check file content hash, should only happens once?!
        # noinspection PyProtectedMember
        file_hash, _ = sha256sum(path, block_size=Artifacts._hash_block_size)
        if file_hash != task.artifacts[self.model_endpoint.preprocess_artifact].hash:
            print("INFO: re-downloading artifact '{}' hash changed".format(
                self.model_endpoint.preprocess_artifact))
            path = task.artifacts[self.model_endpoint.preprocess_artifact].get_local_copy(
                extract_archive=True,
                force_download=True,
            )
        else:
            # extract zip if we need to, otherwise it will be the same
            path = task.artifacts[self.model_endpoint.preprocess_artifact].get_local_copy(
                extract_archive=True,
            )

        import importlib.util
        spec = importlib.util.spec_from_file_location("Preprocess", path)
        _preprocess = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_preprocess)
        Preprocess = _preprocess.Preprocess  # noqa
        # override `send_request` method
        Preprocess.send_request = BasePreprocessRequest._preprocess_send_request
        # create preprocess class
        self._preprocess = Preprocess()
        # custom model load callback function
        if callable(getattr(self._preprocess, 'load', None)):
            self._model = self._preprocess.load(self._get_local_model_file())

    def preprocess(self, request: dict, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Optional[Any]:
        """
        Raise exception to report an error
        Return value will be passed to serving engine

        :param request: dictionary as recieved from the RestAPI
        :param collect_custom_statistics_fn: Optional, allows to send a custom set of key/values
            to the statictics collector servicd

            Usage example:
            >>> print(request)
            {"x0": 1, "x1": 2}
            >>> collect_custom_statistics_fn({"x0": 1, "x1": 2})

        :return: Object to be passed directly to the model inference
        """
        if self._preprocess is not None and hasattr(self._preprocess, 'preprocess'):
            return self._preprocess.preprocess(request, collect_custom_statistics_fn)
        return request

    def postprocess(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Optional[dict]:
        """
        Raise exception to report an error
        Return value will be passed to serving engine

        :param data: object as recieved from the inference model function
        :param collect_custom_statistics_fn: Optional, allows to send a custom set of key/values
            to the statictics collector servicd

            Usage example:
            >>> collect_custom_statistics_fn({"y": 1})

        :return: Dictionary passed directly as the returned result of the RestAPI
        """
        if self._preprocess is not None and hasattr(self._preprocess, 'postprocess'):
            return self._preprocess.postprocess(data, collect_custom_statistics_fn)
        return data

    def process(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Any:
        """
        The actual processing function. Can be send to external service

        :param data: object as recieved from the preprocessing function
        :param collect_custom_statistics_fn: Optional, allows to send a custom set of key/values
            to the statictics collector servicd

            Usage example:
            >>> collect_custom_statistics_fn({"type": "classification"})

        :return: Object to be passed tp the post-processing function
        """
        pass

    def _get_local_model_file(self):
        model_repo_object = Model(model_id=self.model_endpoint.model_id)
        return model_repo_object.get_local_copy()

    @classmethod
    def set_server_config(cls, server_config: dict) -> None:
        cls._server_config = server_config

    @classmethod
    def get_server_config(cls) -> dict:
        return cls._server_config

    @classmethod
    def validate_engine_type(cls, engine: str) -> bool:
        return engine in cls.__preprocessing_lookup

    @classmethod
    def get_engine_cls(cls, engine: str) -> Callable:
        return cls.__preprocessing_lookup.get(engine)

    @staticmethod
    def register_engine(engine_name: str, modules: Optional[List[str]] = None) -> Callable:
        """
        A decorator to register an annotation type name for classes deriving from Annotation
        """

        def wrapper(cls):
            cls.__preprocessing_lookup[engine_name] = cls
            return cls

        if modules:
            BasePreprocessRequest.__preprocessing_modules |= set(modules)

        return wrapper

    @staticmethod
    def load_modules() -> None:
        for m in BasePreprocessRequest.__preprocessing_modules:
            try:
                # silently fail
                import importlib
                importlib.import_module(m)
            except (ImportError, TypeError):
                pass

    @staticmethod
    def _preprocess_send_request(self, endpoint: str, version: str = None, data: dict = None) -> Optional[dict]:
        endpoint = "{}/{}".format(endpoint.strip("/"), version.strip("/")) if version else endpoint.strip("/")
        base_url = BasePreprocessRequest.get_server_config().get("base_serving_url")
        base_url = (base_url or BasePreprocessRequest._default_serving_base_url).strip("/")
        url = "{}/{}".format(base_url, endpoint.strip("/"))
        return_value = request_post(url, json=data, timeout=BasePreprocessRequest._timeout)
        if not return_value.ok:
            return None
        return return_value.json()


@BasePreprocessRequest.register_engine("triton", modules=["grpc", "tritonclient"])
class TritonPreprocessRequest(BasePreprocessRequest):
    _content_lookup = {
        np.uint8: 'uint_contents',
        np.int8: 'int_contents',
        np.int64: 'int64_contents',
        np.uint64: 'uint64_contents',
        np.int: 'int_contents',
        np.uint: 'uint_contents',
        np.bool: 'bool_contents',
        np.float32: 'fp32_contents',
        np.float64: 'fp64_contents',
    }
    _default_grpc_address = "127.0.0.1:8001"
    _ext_grpc = None
    _ext_np_to_triton_dtype = None
    _ext_service_pb2 = None
    _ext_service_pb2_grpc = None

    def __init__(self, model_endpoint: ModelEndpoint, task: Task = None):
        super(TritonPreprocessRequest, self).__init__(
            model_endpoint=model_endpoint, task=task)

        # load Triton Module
        if self._ext_grpc is None:
            import grpc  # noqa
            self._ext_grpc = grpc

        if self._ext_np_to_triton_dtype is None:
            from tritonclient.utils import np_to_triton_dtype  # noqa
            self._ext_np_to_triton_dtype = np_to_triton_dtype

        if self._ext_service_pb2 is None:
            from tritonclient.grpc import service_pb2, service_pb2_grpc  # noqa
            self._ext_service_pb2 = service_pb2
            self._ext_service_pb2_grpc = service_pb2_grpc

    def process(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Any:
        """
        The actual processing function.
        Detect gRPC server and send the request to it

        :param data: object as recieved from the preprocessing function
        :param collect_custom_statistics_fn: Optional, allows to send a custom set of key/values
            to the statictics collector servicd

            Usage example:
            >>> collect_custom_statistics_fn({"type": "classification"})

        :return: Object to be passed tp the post-processing function
        """
        # allow to override bt preprocessing class
        if self._preprocess is not None and hasattr(self._preprocess, "process"):
            return self._preprocess.process(data, collect_custom_statistics_fn)

        # Create gRPC stub for communicating with the server
        triton_server_address = self._server_config.get("triton_grpc_server") or self._default_grpc_address
        if not triton_server_address:
            raise ValueError("External Triton gRPC server is not configured!")
        try:
            channel = self._ext_grpc.insecure_channel(triton_server_address)
            grpc_stub = self._ext_service_pb2_grpc.GRPCInferenceServiceStub(channel)
        except Exception as ex:
            raise ValueError("External Triton gRPC server misconfigured [{}]: {}".format(triton_server_address, ex))

        # Generate the request
        request = self._ext_service_pb2.ModelInferRequest()
        request.model_name = "{}/{}".format(self.model_endpoint.serving_url, self.model_endpoint.version).strip("/")
        # we do not use the Triton model versions, we just assume a single version per endpoint
        request.model_version = "1"

        # take the input data
        input_data = np.array(data, dtype=self.model_endpoint.input_type)

        # Populate the inputs in inference request
        input0 = request.InferInputTensor()
        input0.name = self.model_endpoint.input_name
        input_dtype = np.dtype(self.model_endpoint.input_type).type
        input0.datatype = self._ext_np_to_triton_dtype(input_dtype)
        input0.shape.extend(self.model_endpoint.input_size)

        # to be inferred
        input_func = self._content_lookup.get(input_dtype)
        if not input_func:
            raise ValueError("Input type nt supported {}".format(input_dtype))
        input_func = getattr(input0.contents, input_func)
        input_func[:] = input_data.flatten()

        # push into request
        request.inputs.extend([input0])

        # Populate the outputs in the inference request
        output0 = request.InferRequestedOutputTensor()
        output0.name = self.model_endpoint.output_name

        request.outputs.extend([output0])
        response = grpc_stub.ModelInfer(
            request,
            compression=self._ext_grpc.Compression.Gzip,
            timeout=self._timeout
        )

        output_results = []
        index = 0
        for output in response.outputs:
            shape = []
            for value in output.shape:
                shape.append(value)
            output_results.append(
                np.frombuffer(response.raw_output_contents[index], dtype=self.model_endpoint.output_type))
            output_results[-1] = np.resize(output_results[-1], shape)
            index += 1

        # if we have a single matrix, return it as is
        return output_results[0] if index == 1 else output_results


@BasePreprocessRequest.register_engine("sklearn", modules=["joblib", "sklearn"])
class SKLearnPreprocessRequest(BasePreprocessRequest):
    def __init__(self, model_endpoint: ModelEndpoint, task: Task = None):
        super(SKLearnPreprocessRequest, self).__init__(
            model_endpoint=model_endpoint, task=task)
        if self._model is None:
            # get model
            import joblib  # noqa
            self._model = joblib.load(filename=self._get_local_model_file())

    def process(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Any:
        """
        The actual processing function.
        We run the model in this context
        """
        return self._model.predict(data)


@BasePreprocessRequest.register_engine("xgboost", modules=["xgboost"])
class XGBoostPreprocessRequest(BasePreprocessRequest):
    def __init__(self, model_endpoint: ModelEndpoint, task: Task = None):
        super(XGBoostPreprocessRequest, self).__init__(
            model_endpoint=model_endpoint, task=task)
        if self._model is None:
            # get model
            import xgboost  # noqa
            self._model = xgboost.Booster()
            self._model.load_model(self._get_local_model_file())

    def process(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Any:
        """
        The actual processing function.
        We run the model in this context
        """
        return self._model.predict(data)


@BasePreprocessRequest.register_engine("lightgbm", modules=["lightgbm"])
class LightGBMPreprocessRequest(BasePreprocessRequest):
    def __init__(self, model_endpoint: ModelEndpoint, task: Task = None):
        super(LightGBMPreprocessRequest, self).__init__(
            model_endpoint=model_endpoint, task=task)
        if self._model is None:
            # get model
            import lightgbm  # noqa
            self._model = lightgbm.Booster(model_file=self._get_local_model_file())

    def process(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Any:
        """
        The actual processing function.
        We run the model in this context
        """
        return self._model.predict(data)


@BasePreprocessRequest.register_engine("custom")
class CustomPreprocessRequest(BasePreprocessRequest):
    def __init__(self, model_endpoint: ModelEndpoint, task: Task = None):
        super(CustomPreprocessRequest, self).__init__(
            model_endpoint=model_endpoint, task=task)

    def process(self, data: Any, collect_custom_statistics_fn: Callable[[dict], None] = None) -> Any:
        """
        The actual processing function.
        We run the process in this context
        """
        if self._preprocess is not None and hasattr(self._preprocess, 'process'):
            return self._preprocess.process(data, collect_custom_statistics_fn)
        return None

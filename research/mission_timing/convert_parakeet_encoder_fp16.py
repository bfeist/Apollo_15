"""Create a benchmark copy of Parakeet v3 with an FP16 encoder and unchanged decoder."""

import argparse
from collections import Counter
from hashlib import file_digest
from importlib.metadata import version
import json
from pathlib import Path
import shutil


def main():
    import onnx
    from onnxruntime.transformers.float16 import convert_float_to_float16
    from onnxruntime.transformers.onnx_model import OnnxModel

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    encoder = "encoder-model.onnx"
    input_graph = onnx.load(args.model / encoder, load_external_data=False)
    input_graph = onnx.shape_inference.infer_shapes(input_graph)
    onnx.external_data_helper.load_external_data_for_model(input_graph, str(args.model))
    original_counts = Counter(onnx.TensorProto.DataType.Name(weight.data_type)
                              for weight in input_graph.graph.initializer)
    # Retain the exported float32 I/O. The encoder weights and CUDA arithmetic can
    # use FP16 while the distinct decoder/joint and its interface remain unchanged.
    graph = convert_float_to_float16(input_graph, keep_io_types=True, disable_shape_infer=True)
    OnnxModel(graph).topological_sort()
    counts = Counter(onnx.TensorProto.DataType.Name(weight.data_type) for weight in graph.graph.initializer)
    destination = args.output / encoder
    external = encoder + ".data"
    onnx.save_model(graph, destination, save_as_external_data=True, all_tensors_to_one_file=True,
                    location=external, size_threshold=1024)
    onnx.checker.check_model(str(destination))
    auxiliary = ["config.json", "vocab.txt", "decoder_joint-model.onnx"]
    for name in auxiliary:
        shutil.copyfile(args.model / name, args.output / name)
    files = {}
    for name in [encoder, external, *auxiliary]:
        with (args.output / name).open("rb") as handle:
            files[name] = file_digest(handle, "sha256").hexdigest()
    receipt = {"original_model_directory": str(args.model), "onnx_version": onnx.__version__,
               "converter_onnxruntime_gpu_version": version("onnxruntime-gpu"), "keep_io_types": True,
               "original_encoder_initializers": dict(original_counts), "encoder_initializers": dict(counts),
               "files_sha256": files}
    (args.output / "encoder_precision.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()

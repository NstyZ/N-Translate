"""npu_sanity.py —— 用最小模型验证 OpenVINO NPU 软件栈是否可用（无需下载任何模型）"""
import numpy as np
import openvino as ov
from openvino import opset13 as ops

core = ov.Core()
print("OpenVINO:", ov.__version__)
print("available_devices:", core.available_devices)

x = ops.parameter([1, 4], np.float32, name="x")
w = ops.constant(np.arange(16, dtype=np.float32).reshape(4, 4))
b = ops.constant(np.ones((1, 4), dtype=np.float32))
y = ops.matmul(x, w, False, False) + b
model = ov.Model([y], [x], "tiny")

inp = np.ones((1, 4), dtype=np.float32)

for dev in ["CPU", "NPU"]:
    try:
        if dev not in core.available_devices:
            print(f"{dev}: NOT AVAILABLE")
            continue
        cm = core.compile_model(model, dev)
        out = cm({0: inp})
        print(f"{dev}: OK  result={list(out.values())[0].ravel()}")
    except Exception as e:
        print(f"{dev}: FAIL {type(e).__name__}: {e}")

# 打印 NPU 的详细属性
try:
    if "NPU" in core.available_devices:
        print("\n--- NPU device properties ---")
        for p in ["FULL_DEVICE_NAME", "DEVICE_ARCHITECTURE", "OPTIMIZATION_CAPABILITIES",
                  "NPU_DEVICE_TOTAL_MEM_SIZE", "SUPPORTED_PROPERTIES"]:
            try:
                print(f"  {p}: {core.get_property('NPU', p)}")
            except Exception as e:
                print(f"  {p}: <{type(e).__name__}>")
except Exception as e:
    print("props error:", e)

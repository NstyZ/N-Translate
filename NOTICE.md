# 第三方组件与模型许可

本仓库的**代码**以 [MIT](LICENSE) 发布。下面这些是运行时会用到的第三方组件与模型，
它们各有自己的许可，使用时请遵守。

## 模型

| 组件 | 来源 | 许可 | 说明 |
|---|---|---|---|
| Index-Translate 2B | [IndexTeam/Index-Translate-2B](https://huggingface.co/IndexTeam/Index-Translate-2B)（哔哩哔哩 / Index LLM Team） | **Apache-2.0** | 默认翻译模型，不随仓库分发，由 `install_model.py` 按需下载 |
| OPUS-MT en-zh | [Helsinki-NLP/opus-mt-en-zh](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh) | **CC-BY-4.0** | 轻量模型，由 `setup.bat` 下载并用 OpenVINO 量化导出 |

> 早期版本曾支持 NLLB-200 distilled 600M，因其许可为 **CC-BY-NC-4.0（禁止商用）**，
> 自 v1.0.0 起已从项目中移除，代码里不再包含相关配置。

## 推理与工具链

| 组件 | 许可 | 用途 |
|---|---|---|
| [llama.cpp](https://github.com/ggml-org/llama.cpp) | MIT | 运行 Index-Translate（GGUF），由 `install_llama.py` 下载官方构建 |
| [OpenVINO](https://github.com/openvinotoolkit/openvino) | Apache-2.0 | 运行 OPUS-MT，Intel NPU / CPU / GPU 推理 |
| [optimum-intel](https://github.com/huggingface/optimum-intel) / [NNCF](https://github.com/openvinotoolkit/nncf) | Apache-2.0 | 模型导出与 INT8 量化 |
| [openvino-tokenizers](https://github.com/openvinotoolkit/openvino_tokenizers) | Apache-2.0 | 把 tokenizer 转成 OpenVINO 图（运行时不依赖 transformers） |
| [SentencePiece](https://github.com/google/sentencepiece) | Apache-2.0 | OPUS-MT 的 MarianTokenizer 复刻 |
| [Jinja2](https://palletsprojects.com/p/jinja/) | BSD-3-Clause | 渲染 Index-Translate 的 chat template |

## 再分发提醒

如果你把本项目（或它的分发包）再发布出去：

- 必须保留 [LICENSE](LICENSE) 与 MIT 版权声明；
- 分发 **OPUS-MT** 模型文件时需保留其 CC-BY-4.0 署名；
- 分发 **Index-Translate** 模型文件时需遵守 Apache-2.0（保留其 LICENSE 与 NOTICE）。

# 眼底血管实时分割硬件加速引擎 —— 代码包说明

本包为项目的全部源码与工程脚本。数据集（STARE / DRIVE）与算法运行结果图因体积与
版权原因未包含，可分别从公开渠道获取或由本包脚本重新生成。

## 一句话

在 PYNQ-Z2（Zynq-7020）上，用 Vitis HLS 把眼底血管分割中最耗时的两个算子
（3×3 中值滤波、11×11 黑帽形态学）实现为流式 IP 核，经 AXI DMA 与 PS 端协同，
核心算子单帧 4.476 ms（约 223 fps），较板载 ARM 上运行 OpenCV 加速 37.6 倍，
且输出与软件参考在 423,500 像素上逐像素完全一致。

## 目录结构

| 目录 | 内容 |
|---|---|
| `src/` | 算法软件参考实现（`pipeline.py` 主流程、`evaluate.py` 指标、`data_io.py`、`visualize.py`） |
| `hls/vessel_prj/` | **Vitis HLS 源码**：`vessel_top.cpp/.hpp`（IP 实现）、`tb_vessel.cpp`（测试台）、`run_hls.tcl`（一键 csynth + cosim） |
| `hls/test_vectors/` | C/RTL 协同仿真比对向量，由 `python工具/gen_test_vectors.py` 生成 |
| `vivado_s10/` | **Vivado 系统集成脚本**：`build_bd.tcl` 搭建 Block Design、`verify_bd.tcl` 硬检查（含 DMA 长度位宽） |
| `python工具/` | PS 端驱动与全部实验脚本（见下表） |
| `s11_data_out/` | 板端测试用二进制：605×700 单通道，每帧 423,500 字节 |
| `docs_build/` | 文档构建脚本（`md2docx_template.py` Markdown→Word/PDF、`wordcount_doc.py` 字数校验） |
| `算法详解/` | V1 / V2 / V3 三版算法原理说明 |
| `技术文档/` | 技术文档 Markdown 源（正文、参考文献、成品 PDF） |
| 根目录 `*.md` | 项目过程文档：设计方案、搭建路线、性能指标汇总 |

### `python工具/` 关键脚本

| 脚本 | 用途 |
|---|---|
| `pynq_vessel_driver.py` | 寄存器级驱动：MMIO 直写 + AXI DMA 传输（含启动顺序与完成判据） |
| `demo_s11_board.py` | 板端一键自包含演示：清理重载 bit → 重建 MMIO/Buffer → 跑通对比 |
| `demo_ui_board.py` | 板端网页交互界面（端口 7860）：内置图 / 上传图，软件与硬件双跑对比 |
| `fundus_autoloc.py` | 实拍照片自动眼底定位（R−B 色度分割定位视野圆，替代失效的阈值法） |
| `regress_im0002_board.py` | 单帧正确性回归（与软件真值逐像素比对） |
| `stress*_board.py` | 连续压测（单图 / 双图交替 / 长时 2000 帧） |
| `make_board_data*.py` | 生成板端 `.bin` 测试数据 |
| `gen_test_vectors.py` | 生成 HLS 仿真比对向量 |

## 复现路径

1. **算法原型**（PC，Python）：`src/pipeline.py` + `src/evaluate.py`
2. **HLS 实现与仿真**（Linux，Vitis HLS 2022.2）：
   `cd hls/vessel_prj && vitis_hls -f run_hls.tcl`
   ⚠️ 必须先 csynth 再 cosim；`-reset` 会清空 solution，跑前先备份
3. **系统集成**（Linux，Vivado 2022.2）：`vivado_s10/build_bd.tcl` → 生成 bitstream
4. **上板验证**（PYNQ-Z2）：`python工具/demo_s11_board.py`

## 关键参数

| 项 | 值 |
|---|---|
| 器件 | `xc7z020clg400-1`（PYNQ-Z2） |
| 血管 IP 控制口 | `0x40000000` |
| AXI DMA | `0x41E00000` |
| 图像 | 605 × 700，单帧 423,500 字节 |
| 时钟 | PL 100 MHz |
| DMA 长度位宽 | 26 位（默认 14 位会静默截断，脚本内已强制并加硬检查） |

## 环境

- Vitis HLS 2022.2 / Vivado 2022.2（Linux，工程目录勿放在共享文件夹下）
- PYNQ-Z2 + PYNQ v2.7 镜像（板载 OpenCV 4.5.4、numpy）
- PC 侧 Python 3 + numpy + opencv-python（指标评估与可视化）

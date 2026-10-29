#ifndef VESSEL_TOP_HPP
#define VESSEL_TOP_HPP

/*
 * 眼底血管分割 —— FPGA 加速 IP（规格 v1.0 冻结版，行缓存 streaming 实现）
 *
 * 硬件流水线：
 *   中值滤波 3x3（边界复制） → 黑帽 11x11 矩形核（膨胀→腐蚀，输出原始差值）
 *
 * 实现方式：行缓存 + 滑动寄存器窗 + DATAFLOW 任务级流水，
 *   每个时钟拍处理 1 个像素（II=1），不使用整帧缓存。
 *
 * 接口：
 *   src  : 输入像素流，8-bit，行主序，共 H*W 个像素（CLAHE 后的灰度图）
 *   dst  : 输出像素流，8-bit，黑帽原始差值（未归一化，软件端再做 MINMAX）
 *
 * 边界语义（必须与 OpenCV/Python 参考逐像素一致）：
 *   - 中值 3x3     : BORDER_REPLICATE（复制最近边界像素）
 *   - 膨胀 11x11   : 越界填 0（最小值，max 运算不影响结果）
 *   - 腐蚀 11x11   : 越界填 255（最大值，min 运算不影响结果）
 *   - 黑帽         : erosion(dilation(x)) - x，饱和减法（不为负）
 */

#include <hls_stream.h>
#include <ap_axi_sdata.h>

/* ---------- 冻结参数（规格 v1.0，勿随意改） ---------- */
#define H            605    // STARE 行数
#define W            700    // STARE 列数
#define MED_R        1      // 中值半径：3x3
#define MED_K        3      // 中值窗口边长
#define BH_R         5      // 黑帽半径：11x11
#define BH_K         11     // 黑帽窗口边长
#define PIX_TOTAL    (H * W)

/* AXI4-Stream 数据宽度：8-bit 数据 + 1-bit TLAST（标记一帧最后一个像素） */
typedef ap_axiu<8, 0, 0, 1> axis_u8;

/* 顶层函数 */
void vessel_top(hls::stream<axis_u8> &src, hls::stream<axis_u8> &dst);

#endif // VESSEL_TOP_HPP

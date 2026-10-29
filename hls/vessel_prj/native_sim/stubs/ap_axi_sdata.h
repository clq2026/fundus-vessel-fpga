/*
 * 本地 g++ 验证用的 ap_axi_sdata.h / ap_int 轻量替身。
 * 仅本地仿真用，不加入 Vitis HLS 工程。
 * 提供 ap_axiu<W,0,0,1>：8-bit 数据 + last 标志。
 */
#ifndef LOCAL_AP_AXI_SDATA_H
#define LOCAL_AP_AXI_SDATA_H

#include <cstdint>

template <int W, int _U, int _TI, int _TL>
struct ap_axiu {
    // 只建模本设计用到的字段；用 int 存，方便读写
    int data;
    int keep;
    int strb;
    int last;
};

#endif

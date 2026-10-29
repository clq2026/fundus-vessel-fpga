#include "vessel_top.hpp"

/*
 * ============================================================================
 *  实现说明（对应《硬件算法规格 v1.0》）—— 行缓存 streaming 版
 *
 *  旧「整帧缓存」版每个像素要从大 BRAM 同时读 121 个数（11x11 窗口），
 *  BRAM 读口不够，HLS 调度器搜索空间爆炸（实测综合 1 小时以上无结果）。
 *
 *  本版改为「行缓存 + 滑动窗口 + DATAFLOW 流水线」：
 *    像素逐个流入，靠行缓存和寄存器窗口移位自然凑出窗口，不回头读大内存，
 *    目标 II=1（每个时钟拍出 1 个像素）。
 *
 *  任务级流水线（各任务之间用 hls::stream 连接，DATAFLOW 并行推进）：
 *
 *    AXI读入
 *      -> [补边:复制] -> 中值窗口3x3  -> med ──┬--> [补边:填0] -> 膨胀窗口11x11
 *                                             │                       │
 *                                             └(延迟FIFO 16384)        ↓
 *                                              med_delay  [补边:填255] -> 腐蚀窗口11x11
 *                                                                       │
 *                              黑帽 = 腐蚀结果 − 中值(对齐) <───────────┘
 *      -> AXI输出
 *
 *  边界语义与旧版逐像素一致：
 *    - 中值 BORDER_REPLICATE：用「复制补边」pad 单元实现；
 *    - 膨胀越界=0、腐蚀越界=255：用「常量补边」pad 单元实现。
 *  每个窗口算子本身只在「补边后」的规则光栅上滑窗，不含任何分支，
 *  行为可逐像素核对。
 *
 *  2026-09-28 性能修复（重要）：pad_replicate 由「单行缓存、读满一行再输出一行」
 *    改为「双缓冲 ping-pong」。原版两段因 rowbuf 依赖必须串行 → 平均 2 拍/像素，
 *    而它是流水线第一级，把后面所有算子一起拖慢一倍（协同仿真实测单帧 866656 拍
 *    ≈ 8.67ms，而 csynth 估计只有 436701 拍）。修复后预计 ~426800 拍 ≈ 4.27ms。
 *    边界语义完全不变，已用 Python 参考向量（两图 423500 像素 max_diff=0）
 *    与 pad_replicate 单元自测（BORDER_REPLICATE 解析对比）双重验证。
 * ============================================================================
 */

typedef unsigned char u8;

/* ============================================================================
 *  9 元素中值：奇偶排序网络（36 比较器全排序后取 index 4）
 *  与旧版完全相同，已经数十万组窗口穷举验证。
 * ========================================================================== */
static inline u8 median9(u8 a[9]) {
#pragma HLS INLINE
    u8 t;
#define SW(i, j) if (a[i] > a[j]) { t = a[i]; a[i] = a[j]; a[j] = t; }

    SW(0,1) SW(2,3) SW(4,5) SW(6,7)
    SW(1,2) SW(3,4) SW(5,6) SW(7,8)
    SW(0,1) SW(2,3) SW(4,5) SW(6,7)
    SW(1,2) SW(3,4) SW(5,6) SW(7,8)
    SW(0,1) SW(2,3) SW(4,5) SW(6,7)
    SW(1,2) SW(3,4) SW(5,6) SW(7,8)
    SW(0,1) SW(2,3) SW(4,5) SW(6,7)
    SW(1,2) SW(3,4) SW(5,6) SW(7,8)
    SW(0,1) SW(2,3) SW(4,5) SW(6,7)
#undef SW
    return a[4];
}

/* ============================================================================
 *  补边单元 1：边界复制（中值用）—— 双缓冲 ping-pong 版（2026-09-28 性能修复）
 *
 *  把 HxW 输入流扩展成 (H+2R)x(W+2R) 的规则流：
 *    上/下各补 R 行 = 第 1/最后 1 行的复制；
 *    左/右各补 R 列 = 该行首/尾像素的复制。
 *
 *  【性能要点 —— 旧版瓶颈就在这里】
 *  旧版写成：
 *      if (sr > cur_sr) { read_row: 读满一整行 }   // 700 拍
 *      col_loop:        输出一整行                 // 702 拍
 *  两段因 rowbuf 的数据依赖而**必须串行**（HLS 无法重叠），
 *  于是每输出一行花 ~1402 拍、只产出 702 个像素 → 平均 2 拍/像素。
 *  它又是整条流水线的第一级，会把后面的中值/膨胀/腐蚀全部拖慢一倍
 *  （实测单帧 866656 拍 ≈ 8.67ms，而 csynth 乐观估计只有 436701 拍）。
 *
 *  本版改为**双缓冲**：输出第 pr 行的同时，在同一个 col_loop 里
 *  把第 pr+1 行所需的源行预读到另一块 rowbuf（两块互不冲突，可 II=1），
 *  每行只剩 ~702 拍 → 恢复约 1 拍/像素（预计单帧 ~426800 拍 ≈ 4.27ms）。
 *
 *  缓冲调度：
 *    - 先把源第 0 行读入 rowbuf[0]（顶部 R 个复制行 + 第 0 行本身都要用它）；
 *    - cur 指向"当前输出行"所用缓冲，loaded 记录已读入的最大源行号；
 *    - 每轮先算本行所需源行 sr = clamp(pr-R) 与下一轮所需 sr_next = clamp(pr+1-R)；
 *      若 sr_next > loaded 则在输出本行的同时把源行 sr_next 读进 rowbuf[1-cur]，
 *      轮末再 cur = 1-cur。上下边界行会被 clamp 到同一行，因此不会重复读。
 *  边界语义与旧版完全一致（逐像素相同，已用 Python 参考向量验证）。
 * ========================================================================== */
static void pad_replicate(hls::stream<u8> &in, hls::stream<u8> &out) {
    const int R  = MED_R;
    const int PW = W + 2 * R;
    const int PH = H + 2 * R;

    u8 rowbuf[2][W];
    // 行维完全拆开 -> 两个独立行存储；两端各一拍一读一写，用双端口 RAM
#pragma HLS ARRAY_PARTITION variable=rowbuf complete dim=1
#pragma HLS BIND_STORAGE variable=rowbuf type=RAM_2P

    // 预读源第 0 行
    read_row0: for (int c = 0; c < W; c++) {
#pragma HLS PIPELINE II=1
        rowbuf[0][c] = in.read();
    }

    int cur    = 0;   // 当前输出行所在的缓冲
    int loaded = 0;   // 已读入的最大源行号

    row_loop: for (int pr = 0; pr < PH; pr++) {
        // 本行对应的源行（复制钳位）
        int sr = pr - R;
        if (sr < 0) sr = 0;
        if (sr >= H) sr = H - 1;

        // 下一行对应的源行（决定是否需要预读；同样钳位）
        int sr_next = pr + 1 - R;
        if (sr_next < 0) sr_next = 0;
        if (sr_next >= H) sr_next = H - 1;

        bool do_read = (sr_next > loaded);

        // 输出一整行（左 R 个首像素复制 + 原行 + 右 R 个尾像素复制）
        // 同时把下一行要用的源行预读进另一块缓冲 —— 读/写落在不同 RAM，同拍可完成
        col_loop: for (int pc = 0; pc < PW; pc++) {
#pragma HLS PIPELINE II=1
            int sc = pc - R;
            if (sc < 0) sc = 0;
            if (sc >= W) sc = W - 1;
            out.write(rowbuf[cur][sc]);

            if (do_read && pc < W)
                rowbuf[1 - cur][pc] = in.read();
        }

        if (do_read) {
            cur    = 1 - cur;
            loaded = sr_next;
        }
    }
}

/* ============================================================================
 *  补边单元 2：常量填充（膨胀填 0 / 腐蚀填 255）
 *
 *  把 HxW 输入流扩展成 (H+2R)x(W+2R) 规则流：
 *  图像区域照搬输入，越界位置直接输出常量 FILL，不需要读输入。
 * ========================================================================== */
template <int R, u8 FILL>
static void pad_const(hls::stream<u8> &in, hls::stream<u8> &out) {
    const int PW = W + 2 * R;
    const int PH = H + 2 * R;

    row_loop: for (int pr = 0; pr < PH; pr++) {
        col_loop: for (int pc = 0; pc < PW; pc++) {
#pragma HLS PIPELINE II=1
            int sr = pr - R;
            int sc = pc - R;
            if (sr >= 0 && sr < H && sc >= 0 && sc < W)
                out.write(in.read());   // 图像区域：严格按行主序读
            else
                out.write(FILL);        // 越界：常量
        }
    }
}

/* ============================================================================
 *  窗口归约器：拿到完整 KxK 窗口，输出一个结果
 *  - RedMedian : 3x3 中位数（排序网络）
 *  - RedMax    : 11x11 最大值（膨胀）
 *  - RedMin    : 11x11 最小值（腐蚀）
 * ========================================================================== */
struct RedMedian {
    static inline u8 reduce(u8 w[MED_K][MED_K]) {
#pragma HLS INLINE
        u8 a[9];
#pragma HLS ARRAY_PARTITION variable=a complete
        int k = 0;
        for (int r = 0; r < MED_K; r++)
            for (int c = 0; c < MED_K; c++) {
#pragma HLS UNROLL
                a[k++] = w[r][c];
            }
        return median9(a);
    }
};

struct RedMax {
    static inline u8 reduce(u8 w[BH_K][BH_K]) {
#pragma HLS INLINE
        u8 mx = 0;
        for (int r = 0; r < BH_K; r++)
            for (int c = 0; c < BH_K; c++) {
#pragma HLS UNROLL
                u8 v = w[r][c];
                if (v > mx) mx = v;
            }
        return mx;
    }
};

struct RedMin {
    static inline u8 reduce(u8 w[BH_K][BH_K]) {
#pragma HLS INLINE
        u8 mn = 255;
        for (int r = 0; r < BH_K; r++)
            for (int c = 0; c < BH_K; c++) {
#pragma HLS UNROLL
                u8 v = w[r][c];
                if (v < mn) mn = v;
            }
        return mn;
    }
};

/* ============================================================================
 *  通用滑窗核心（补边后的规则光栅上工作）
 *
 *  消费 (H+2R)x(W+2R) 个像素，输出 HxW 个结果：
 *    lbuf : 2R 条行缓存（按行维拆成独立 RAM，2 端口支持同拍读+写）；
 *    win  : KxK 寄存器窗口，每拍整体左移 1 列、右列从行缓存补入。
 *  装完输入像素 (pr,pc) 后，窗口覆盖行 [pr-2R..pr]、列 [pc-2R..pc]，
 *  中心 = (pr-R, pc-R)；当 pr>=2R 且 pc>=2R 时输出中心结果。
 * ========================================================================== */
template <int R, typename RED>
static void window_core(hls::stream<u8> &in, hls::stream<u8> &out) {
    const int K  = 2 * R + 1;
    const int PW = W + 2 * R;
    const int PH = H + 2 * R;

    u8 lbuf[2 * R][PW];
    // 行维完全拆分成独立行存储；每行需同拍读旧值+写新值 -> 双端口 RAM
#pragma HLS ARRAY_PARTITION variable=lbuf dim=1 complete
#pragma HLS BIND_STORAGE variable=lbuf type=RAM_2P

    u8 win[2 * R + 1][2 * R + 1];
#pragma HLS ARRAY_PARTITION variable=win complete dim=0

    row_loop: for (int pr = 0; pr < PH; pr++) {
        col_loop: for (int pc = 0; pc < PW; pc++) {
#pragma HLS PIPELINE II=1
            u8 pix = in.read();

            // 1) 窗口整体左移一列
            for (int rr = 0; rr < K; rr++)
                for (int cc = 0; cc < K - 1; cc++) {
#pragma HLS UNROLL
                    win[rr][cc] = win[rr][cc + 1];
                }

            // 2) 新右列：前 2R 行取自行缓存（旧值），最后 1 行用当前像素
            for (int rr = 0; rr < 2 * R; rr++) {
#pragma HLS UNROLL
                win[rr][K - 1] = lbuf[rr][pc];
            }
            win[K - 1][K - 1] = pix;

            // 3) 行缓存垂直旋转（先读后写，顺序不可换）
            for (int rr = 0; rr < 2 * R - 1; rr++) {
#pragma HLS UNROLL
                lbuf[rr][pc] = lbuf[rr + 1][pc];
            }
            lbuf[2 * R - 1][pc] = pix;

            // 4) 窗口中心落进原图区域 -> 输出一个结果
            if (pr >= 2 * R && pc >= 2 * R)
                out.write(RED::reduce(win));
        }
    }
}

/* ============================================================================
 *  分叉任务：中值结果同时送给膨胀路径和延迟对齐路径
 * ========================================================================== */
static void fork2(hls::stream<u8> &in,
                  hls::stream<u8> &a, hls::stream<u8> &b) {
    fork_loop: for (int i = 0; i < PIX_TOTAL; i++) {
#pragma HLS PIPELINE II=1
        u8 v = in.read();
        a.write(v);
        b.write(v);
    }
}

/* ============================================================================
 *  黑帽任务：腐蚀结果 − 对齐后的中值（饱和减）
 *  注意先读 closed 再读 med_delay：closed[i] 到达时，中值像素早已在延迟
 *  FIFO 中（FIFO 深度 16384 > 两级 11x11 窗口填充延迟约 2x3555）。
 * ========================================================================== */
static void blackhat_task(hls::stream<u8> &closed,
                          hls::stream<u8> &med_delay,
                          hls::stream<u8> &out) {
    bh_loop: for (int i = 0; i < PIX_TOTAL; i++) {
#pragma HLS PIPELINE II=1
        u8 c = closed.read();
        u8 m = med_delay.read();
        out.write((c > m) ? (u8)(c - m) : (u8)0);
    }
}

/* ============================================================================
 *  AXI 适配：axis_u8 <-> u8
 * ========================================================================== */
static void axis_in_task(hls::stream<axis_u8> &src, hls::stream<u8> &out) {
    in_loop: for (int i = 0; i < PIX_TOTAL; i++) {
#pragma HLS PIPELINE II=1
        axis_u8 p = src.read();
        out.write((u8)p.data);
    }
}

static void axis_out_task(hls::stream<u8> &in, hls::stream<axis_u8> &dst) {
    out_loop: for (int i = 0; i < PIX_TOTAL; i++) {
#pragma HLS PIPELINE II=1
        u8 v = in.read();
        axis_u8 p;
        p.data = v;
        p.keep = 1;
        p.strb = 1;
        p.last = (i == PIX_TOTAL - 1) ? 1 : 0;
        dst.write(p);
    }
}

/* ============================================================================
 *  顶层：组装任务级流水线
 * ========================================================================== */
void vessel_top(hls::stream<axis_u8> &src, hls::stream<axis_u8> &dst) {
#pragma HLS INTERFACE axis port=src
#pragma HLS INTERFACE axis port=dst
#pragma HLS INTERFACE s_axilite port=return

    /* ---- 内部数据流 ---- */
    hls::stream<u8> s_in;    // AXI 解包后的原始像素
    hls::stream<u8> s_padm;  // 中值：复制补边后
    hls::stream<u8> s_med;   // 中值结果
    hls::stream<u8> s_mdil;  // 中值 -> 膨胀路径
    hls::stream<u8> s_mdel;  // 中值 -> 延迟对齐路径
    hls::stream<u8> s_padd;  // 膨胀：填 0 补边后
    hls::stream<u8> s_dil;   // 膨胀结果
    hls::stream<u8> s_pade;  // 腐蚀：填 255 补边后
    hls::stream<u8> s_clo;   // 腐蚀结果
    hls::stream<u8> s_bh;    // 黑帽结果

    /* 中值延迟 FIFO：容纳两级 11x11 窗口填充带来的像素相位差
       （每级最大约 R*(W+2R)+R = 3555，两级 < 7200；取 16384 留足余量）。
       深度不够会导致流水线死锁，过深只多占 BRAM（约 8 块）。 */
#pragma HLS STREAM variable=s_mdel depth=16384

#pragma HLS DATAFLOW
    axis_in_task(src, s_in);
    pad_replicate(s_in, s_padm);
    window_core<MED_R, RedMedian>(s_padm, s_med);
    fork2(s_med, s_mdil, s_mdel);

    pad_const<BH_R, 0>(s_mdil, s_padd);
    window_core<BH_R, RedMax>(s_padd, s_dil);

    pad_const<BH_R, 255>(s_dil, s_pade);
    window_core<BH_R, RedMin>(s_pade, s_clo);

    blackhat_task(s_clo, s_mdel, s_bh);
    axis_out_task(s_bh, dst);
}

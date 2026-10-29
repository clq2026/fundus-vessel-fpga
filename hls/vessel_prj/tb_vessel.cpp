/*
 * C 仿真 / 本地验证通用 testbench
 * --------------------------------------------------------------------------
 * 做两件事：
 *   1) 读入 <id>_input_clahe.txt（605x700，行主序，空格分隔），送入 IP；
 *   2) 读出 IP 输出，与 <id>_ref_blackhat_raw.txt 逐像素对比。
 *
 * 在 Vitis HLS 中：把本文件设为 Test Bench，C Simulation 直接运行。
 * 在本地 g++ 中：配合 native_sim/stubs 里的头文件替身编译（见 run_native_sim）。
 *
 * 测试向量路径默认取环境变量 TV_DIR，否则用相对路径 ../test_vectors。
 */
#include "vessel_top.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

static const char *IMG_IDS[2] = {"im0082", "im0002"};

static std::string tv_dir() {
    const char *e = std::getenv("TV_DIR");
    return e ? std::string(e) : std::string("../test_vectors");
}

/* 读一个 605x700 的 txt 到一维数组 */
static bool load_txt(const std::string &path, unsigned char *buf) {
    FILE *f = std::fopen(path.c_str(), "r");
    if (!f) { std::printf("  [错误] 打不开 %s\n", path.c_str()); return false; }
    for (int i = 0; i < PIX_TOTAL; i++) {
        int v;
        if (std::fscanf(f, "%d", &v) != 1) {
            std::printf("  [错误] %s 第 %d 个数据读取失败\n", path.c_str(), i);
            std::fclose(f);
            return false;
        }
        buf[i] = (unsigned char)v;
    }
    std::fclose(f);
    return true;
}

static int test_one(const std::string &dir, const char *id) {
    std::printf("\n--- 测试 %s ---\n", id);
    static unsigned char in_buf[PIX_TOTAL];
    static unsigned char ref_buf[PIX_TOTAL];
    static unsigned char out_buf[PIX_TOTAL];

    std::string in_path  = dir + "/" + id + "_input_clahe.txt";
    std::string ref_path = dir + "/" + id + "_ref_blackhat_raw.txt";

    if (!load_txt(in_path, in_buf)) return 1;
    if (!load_txt(ref_path, ref_buf)) return 1;

    // 送入流
    hls::stream<axis_u8> in_stream;
    hls::stream<axis_u8> out_stream;
    for (int i = 0; i < PIX_TOTAL; i++) {
        axis_u8 p;
        p.data = in_buf[i];
        p.keep = 1; p.strb = 1;
        p.last = (i == PIX_TOTAL - 1) ? 1 : 0;
        in_stream.write(p);
    }

    // 调用被测 IP
    vessel_top(in_stream, out_stream);

    // 收集输出
    for (int i = 0; i < PIX_TOTAL; i++) {
        axis_u8 p = out_stream.read();
        out_buf[i] = (unsigned char)p.data;
    }

    // 逐像素对比（允许误差 0）
    long mismatch = 0;
    int max_diff = 0;
    int first_r = -1, first_c = -1;
    for (int i = 0; i < PIX_TOTAL; i++) {
        int d = (int)out_buf[i] - (int)ref_buf[i];
        if (d < 0) d = -d;
        if (d > max_diff) max_diff = d;
        if (d != 0) {
            if (mismatch == 0) { first_r = i / W; first_c = i % W; }
            mismatch++;
        }
    }

    if (mismatch == 0) {
        std::printf("  [通过] %d 个像素全部一致 (max_diff=0)\n", PIX_TOTAL);
        return 0;
    } else {
        double rate = 100.0 * mismatch / PIX_TOTAL;
        std::printf("  [失败] 不一致像素 %ld/%d (%.4f%%), max_diff=%d, 首个差异@(%d,%d)\n",
                    mismatch, PIX_TOTAL, rate, max_diff, first_r, first_c);
        return 1;
    }
}

int main() {
    std::string dir = tv_dir();
    std::printf("测试向量目录: %s\n", dir.c_str());

    int fail = 0;
    for (int t = 0; t < 2; t++)
        fail += test_one(dir, IMG_IDS[t]);

    std::printf("\n==================== 总结 ====================\n");
    if (fail == 0) std::printf("全部通过  C 仿真输出与 Python 参考逐像素一致\n");
    else           std::printf("存在失败  请按首个差异坐标排查边界/窗口逻辑\n");
    return fail;
}

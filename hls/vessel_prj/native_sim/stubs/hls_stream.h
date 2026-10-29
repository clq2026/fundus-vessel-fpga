/*
 * 本地 g++ 验证用的 hls_stream.h 轻量替身。
 * 仅用于在没有 Vitis HLS 的电脑上用普通 g++ 验证算法逻辑，
 * 不加入 Vitis HLS 工程（HLS 会用自己的官方头文件）。
 * 对齐官方用法：hls::stream<T>
 */
#ifndef LOCAL_HLS_STREAM_H
#define LOCAL_HLS_STREAM_H

#include <deque>
#include <cassert>

namespace hls {

template <typename T>
class stream {
public:
    T read() {
        assert(!q.empty());
        T v = q.front(); q.pop_front();
        return v;
    }
    void write(const T &v) { q.push_back(v); }
    bool empty() const { return q.empty(); }
private:
    std::deque<T> q;
};

} // namespace hls

#endif

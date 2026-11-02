// Swift から NDI SDK の型 (送信の設定・映像のフレーム・動的読み込みの関数表) を使うためのブリッジ。
// libndi はリンクせず、実行時に dlopen して NDIlib_v5_load の関数表を使う (main.swift)
#include <Processing.NDI.Lib.h>

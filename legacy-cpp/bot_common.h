#pragma // bot_common.h
#pragma once
#include <string>
#include <vector>

using namespace std;

// ===== 全局配置（原来 main.cpp 顶部的 struct，搬到这里）=====
struct Config {
    string apiKey, model, napcatUrl, napcatToken, selfId;
    string searchBackend = "tavily";
    string searchApiKey = "";
    int port = 8080;
    int maxHistory = 10;
};

// ===== 历史消息结构 =====
struct Msg {
    string role;
    string content;
};

// ===== 全局变量声明（定义在 main.cpp 里，其他文件 extern 引用）=====
extern Config g_cfg;
extern string g_exeDir;


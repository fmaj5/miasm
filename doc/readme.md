几个元素 

all_mn 所有指令类型
all_mn_mode[mode] 所有指定 mode 的指令类型
all_mn_name[name] 所有指定 name 的指令类型

bintree 树结构, 每个节点是一个指令字段

armtop 流程
1. armtop 会调用 type 动态生成类型, 然后 meatclass 被调用, 处理
2. 生成一个新的指令类
3. 处理后添加到 all_mn 这些静态成员
4. 生成的实例, 放在 all_mn_inst 中
5. 添加到 bintree



dis 流程

1. guess_mnomo 猜测指令, 得到候选指令
1. 对每个候选指令的字段们 decode, 失败就下一个候选指令
1. 成功, 对每个指令 args, 应用 expr_simp
1. 调用 instruction 生成指令文本类实例

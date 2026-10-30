"""VASPKIT 接入：从一个 VESTA 导出的 .vasp 到一条可执行的计算链。

本包按「越靠前越确定」排列：structure/recipe/verify/stages 全是纯函数，
不碰网络；真正调用 VASPKIT 的适配层单独成文件，便于审计。
"""

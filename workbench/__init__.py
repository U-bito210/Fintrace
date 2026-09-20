"""CARD-15 §4 —— Demo 页面的容器包。

本包与仓库根的 ``workbench.py`` 同名但**不冲突**：入口是**脚本**，由
``streamlit run workbench.py`` 直接执行（``AppTest.from_file`` 同理），从不被
import；本包只承载新增的 Demo 页面，既有页面函数一行未动。全仓库没有任何
``import workbench``（仅 ``workbench_service`` 等同名前缀模块，那是另一个名字）。
"""

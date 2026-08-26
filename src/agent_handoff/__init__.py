#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent-handoff — 把一个代码仓库的当前状态固化成"可无损接续"的交接现场。

用途：Codex / Claude Code 会话因上游 400、供应商熔断、上下文超限等原因卡死时，
在新会话开始前运行本工具，一次完成三步：
  1. 提交快照（自动排除计划文档声明为"用户私有"的文件）
  2. 按客观证据回填计划文档的复选框
  3. 生成交接 Markdown + 新会话开场提示词

设计原则：不硬编码任何项目名、路径、任务名或测试命令。
项目相关信息全部从仓库自身推断：
  git 元数据               -> 分支 / HEAD / 未提交改动
  pyproject / package.json -> 技术栈与测试命令
  计划文档 Files: 段        -> 每个任务应产出哪些文件
  计划文档 Interfaces: 段   -> 每个任务应产出哪些符号
  计划文档 约束段           -> 哪些文件不得提交
"""
from __future__ import annotations

__version__ = "2.8.2"
__all__ = ["__version__"]

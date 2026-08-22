# 群聊定向消息方案设计

## 1. 背景与问题

### 问题描述
Boss 在终端直接对成员 pane 说的话，出现在了群瀑布流（账本 kind=me 消息）。

### 取证结论
- **唯一写入口**: POST `/api/chat/sessions/{name}/mail`（前端唯一调用方是群聊 Composer）
- **不存在的路径**: interact 弹窗、herdr attach/harvest、hook 链均不写账本
- **结论**: 代码里不存在终端输入自动进群的通道，那条入库必来自显式 API 调用

### 调用链验证
```
前端 GroupChatPage.tsx:649-707
  → sendSessionMail(activeSession, text, mailTo, {delivery})
API chatSession.ts:221-240
  → POST /api/chat/sessions/{session}/mail
后端 server.py:6770-6875
  → hub_client.overseer_send(recipients=dest, ...)
Hub hub_client.py:521-553
  → POST {HUB}/mail/{project}/overseer/send
```

**确认**: 定向消息 (@某人) 的收件人列表会完整传给 overseer_send，进入 Hub 邮箱系统。

---

## 2. 最终方案

### 方案选择：方案 A
**定向消息 3.0 不进 Hub，仅 Cockpit 账本 + pane 通知**

#### 选择理由
1. **改动最小** - api_chat_session_mail 里 overseer_send 对 direct 消息跳过即可
2. **pane 通知路径独立** - _notify_chat_recipients 本来就不依赖 Hub，投递不受影响
3. **4.0 迁移零负担** - 不增加 Hub 复杂度，没有要迁的定向消息数据
4. **直接解决痛点** - 私聊内容不进公共视图

#### 备选方案（未采纳）
- **方案 B**: 定向消息进 Hub 但作为独立消息类型 - 4.0 话题，现在是过度设计
- **方案 C**: 定向消息映射为 topic 消息 - 过度抽象，3.0 锁里不允许动取数主路

---

## 3. 技术实现

### 3.1 source 打标
**目标**: 区分消息来源（composer/h5/api），解决"不翻日志就能看出每条群消息从哪发的"

#### 实现细节
- **后端**: `ChatMailReq` 添加可选字段 `source: str | None`
- **账本**: 消息记录添加 `source` 字段
- **前端**:
  - Composer 发送时传 `source="composer"`
  - H5 发送时传 `source="h5"`
  - 瀑布流显示来源徽章
- **兼容性**: 旧数据 `source=None`，不影响现有功能

#### 与 Hub 关系
- **无关联**: 纯账本字段，不传给 Hub
- **4.0 冲突**: 无

---

### 3.2 @all 支持
**目标**: 支持 @all/@所有人 显式广播

#### 现状
- **@多人已支持**: `parseMentionTargets` 正则抓所有 @token，逐个匹配成员
- **@all 不存在**: 无 @all/@所有人 token；不 @ 人时默认只发 Leader

#### 实现细节
- **解析层**: `web/features/group-chat/model.ts:168 parseMentionTargets` 添加 `@all`/`@所有人` token 识别
- **账本存储**: `to` 字段存 `["all"]` 标记（**不展开名单**）
- **投递逻辑**: 投递时按**当前成员**展开
- **历史消息**: 成员变动后，历史 `@all` 消息仍显示 `@all`，不会失真为"旧成员名单"

#### 消息分档
1. **@all** - 显式广播，全员可见，进公共瀑布流
2. **@一人/@多人** - 定向消息，投递给被 @ 者，可见性待 Boss 拍板
3. **无 @** - 维持现状发 Leader

---

### 3.3 direct 可见性过滤
**目标**: 定向消息（@单人/@多人）不进公共瀑布流，仅对 Boss 和被 @ 成员可见

#### 实现细节
- **后端**: 账本添加 `direct: bool` 标记字段
  - `direct=True` 当 `to` 不为空且不为 `["all"]`
  - 后端**全量返回**账本，不过滤
- **前端过滤**: `GroupChatPage` 根据当前视角过滤
  ```typescript
  // 伪代码
  const visibleMessages = messages.filter(msg => {
    if (!msg.direct) return true; // 公共消息全员可见
    if (currentUser === 'Boss') return true; // Boss 看到所有消息
    if (msg.to.includes(currentUser)) return true; // 被 @ 者可见
    return false;
  });
  ```
- **H5/桌面**: 逻辑一致

#### 优势
- **后端零改动**: 不需要修改 API 返回逻辑
- **逻辑统一**: 所有客户端使用相同过滤规则

---

### 3.4 interact 私聊入口显性化
**目标**: 让私聊通道更明显，避免误用群聊发私密内容

#### 现状
- `interact` 弹窗通过 `sendPane` 直发 pane，不写账本
- 入口隐蔽，用户不清楚私聊和群聊的区别

#### 实现细节
- **成员头像菜单**: 添加"私聊"和"群聊消息"两个明确按钮
- **私聊**: 调用 `sendPane`，不写账本，不进瀑布流
- **群聊消息**: 调用 `sendSessionMail`，写账本，按 direct 标记控制可见性

---

## 4. 与 4.0 的关系

### 潜在冲突点（方案 A 已规避）
1. **消息分流混乱** - Hub 按 topic/handoff 分流时，定向消息语义不清
2. **收件人语义重载** - `recipients` 字段同时表示"定向对象"和"Hub 收件人"
3. **Hub 端处理逻辑** - Hub 无法区分"群聊定向"和"topic 消息"
4. **数据同步问题** - 账本和 Hub 可能语义分歧

### 方案 A 规避方式
- **定向消息不进 Hub** - 避免所有 Hub 端冲突
- **账本独立管理** - 3.0 账本结构不影响 4.0 Hub 改造
- **迁移成本零** - 4.0 不需要迁移定向消息数据

---

## 5. 待 Boss 拍板

### 5.1 direct 可见性细节
- 当前方案：定向消息仅对 Boss 和被 @ 成员可见
- 待确认：
  - 是否需要"可见性开关"（让用户选择定向消息是否进公共流）
  - 多人定向（@A @B @C）是否形成"小圈可见"

### 5.2 终端输入捕获（可选）
- **不做理由**: 产品上如果要"终端直说也留痕进群"，需要显式做 pane 输入行捕获，不能靠不明路径
- **推迟到 4.0**: 本轮不做，等 4.0 再议

---

## 6. 实施检查清单

### Phase 1: source 打标（独立小步）
- [ ] 后端：`ChatMailReq` 添加 `source` 字段
- [ ] 账本：消息记录添加 `source`
- [ ] 前端：Composer/H5 传 `source` 参数
- [ ] 前端：瀑布流显示来源徽章
- [ ] 测试：验证旧数据兼容性

### Phase 2: @all 支持
- [ ] 前端：`parseMentionTargets` 添加 `@all` token
- [ ] 账本：`to` 存储 `["all"]` 标记
- [ ] 投递：按当前成员展开
- [ ] 测试：成员变动后历史消息不失真

### Phase 3: direct 可见性
- [ ] 账本：添加 `direct` 标记字段
- [ ] 后端：跳过 `overseer_send` for `direct=True`
- [ ] 前端：GroupChatPage 按视角过滤
- [ ] H5：同步过滤逻辑
- [ ] 测试：Boss/被@者/其他成员分别验证可见性

### Phase 4: 私聊入口
- [ ] 前端：成员头像菜单添加"私聊"/"群聊消息"按钮
- [ ] 交互：明确两种消息的区别
- [ ] 测试：用户体验验证

---

## 7. 风险与限制

### 已知限制
1. **历史消息**: 现有群聊消息无 `source`/`direct` 标记，需要容错处理
2. **多客户端同步**: 前端过滤依赖客户端逻辑，需确保 Web/H5/Desktop 一致
3. **Boss 定义**: 当前 Boss 识别逻辑未明确，需确认

### 风险缓解
1. **渐进式上线**: Phase 1-4 可独立上线，互不阻塞
2. **降级方案**: `source`/`direct` 字段为空时，降级为现有行为
3. **充分测试**: 每个 Phase 完成后独立测试

---

## 8. 参考资料

### 代码路径
- 前端解析: `web/features/group-chat/model.ts:168`
- 前端发送: `web/features/group-chat/GroupChatPage.tsx:649-707`
- API 路由: `web/api/src/routes/chatSession.ts:221-240`
- 后端处理: `server.py:6770-6875`
- Hub 客户端: `agent_cockpit/hub_client.py:521-553`

### 相关讨论
- Agent Mail #4357, #4358, #4359, #4360, #4363 (EmeraldBeacon ↔ BoldGrove)

### 方案演进
- 2026-08-21: EmeraldBeacon 取证 + 初版方案
- 2026-08-21: BoldGrove 评审 + 4.0 冲突分析
- 2026-08-21: 双方一致推荐方案 A + 三个细节
- 2026-08-21: Boss 要求整理到 wiki

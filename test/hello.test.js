// dashboard.test.js — 校验构建产物 dist/ 的仪表盘内容
// 从 hello world 骨架测试演进：页面已重写为 aimonitor 仪表盘
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const DIST = path.join(__dirname, "..", "dist");
const AiTrend = require(path.join(__dirname, "..", "src", "js", "trend.js"));

function mustExist(rel) {
  const full = path.join(DIST, rel);
  assert.ok(fs.existsSync(full), `缺少构建产物: ${rel}`);
}

mustExist("index.html");
mustExist("css/style.css");
mustExist("js/main.js");
mustExist("js/trend.js");

const html = fs.readFileSync(path.join(DIST, "index.html"), "utf8");
assert.ok(html.includes("aimonitor"), "dist/index.html 应包含 aimonitor");
assert.ok(html.includes("project-select"), "dist/index.html 应包含项目选择器");
assert.ok(html.includes("task-table"), "dist/index.html 应包含任务表格");
assert.ok(html.includes("status-bars"), "dist/index.html 应包含统计条");
assert.ok(html.includes("refresh-btn"), "dist/index.html 应包含刷新按钮");
assert.ok(html.includes("trend-charts"), "dist/index.html 应包含趋势图容器 (TASK-027)");
assert.ok(html.includes("js/trend.js"), "dist/index.html 应引用 trend.js (TASK-027)");

const js = fs.readFileSync(path.join(DIST, "js/main.js"), "utf8");
assert.ok(js.includes("/api/status"), "dist/js/main.js 应请求 /api/status");
assert.ok(js.includes("/api/history"), "dist/js/main.js 应请求 /api/history (TASK-027)");
assert.ok(js.includes("aimonitor-theme"), "dist/js/main.js 应含主题持久化 key (TASK-028)");
assert.ok(js.includes("prefers-color-scheme"), "dist/js/main.js 应跟随系统偏好 (TASK-028)");
assert.ok(js.includes("sidebar-mask"), "dist/js/main.js 应含移动端抽屉逻辑 (TASK-030)");
assert.ok(html.includes("data-theme"), "dist/index.html 应含主题属性 (TASK-028)");
assert.ok(html.includes("menu-toggle"), "dist/index.html 应含移动端汉堡按钮 (TASK-030)");
assert.ok(html.includes("sidebar-mask"), "dist/index.html 应含移动端侧栏遮罩 (TASK-030)");

// TASK-043: 实例级展示——消费 group/transport（同 group 多实例一行一实例 + agent 传输/离线上下文）
assert.ok(js.includes("p.group"), "dist/js/main.js 应消费 group（实例分组）");
assert.ok(js.includes('p.transport === "agent"'), "dist/js/main.js 应消费 transport（agent 传输标识）");
assert.ok(js.includes("ov-group"), "dist/js/main.js 应渲染实例组徽章");
assert.ok(js.includes("ov-agent"), "dist/js/main.js 应渲染 agent 传输徽章");
// MED-001 回归（REVIEW-2026-08-17-TASK-043）：项目总览表名称单元格必须追加实例 id
// （同 group 同 name 实例行可区分；nameWrap 仅 renderProjectTable 使用，此断言专门捕获
// 总览表实例区分缺口，侧栏/选择器用 label 变量不满足该片段）
assert.ok(js.includes("nameWrap.textContent = p.name + (p.group"),
  "MED-001: 项目总览表名称单元格应追加实例 id（同 group 同 name 实例可区分）");

const css = fs.readFileSync(path.join(DIST, "css/style.css"), "utf8");
assert.ok(css.includes("@media (max-width: 768px)"), "dist/css/style.css 应含移动端响应式断点 (TASK-030)");
assert.ok(css.includes("sidebar.open"), "dist/css/style.css 应含抽屉展开样式 (TASK-030)");
assert.ok(css.includes(".project-table .ov-group"), "dist/css/style.css 应含实例组徽章样式 (TASK-043)");
assert.ok(css.includes(".project-table .ov-agent"), "dist/css/style.css 应含 agent 徽章样式 (TASK-043)");

// CRIT-001 回归（REVIEW-2026-08-15-TASK-030）：基础 .menu-toggle{display:none} 必须位于
// 移动端媒体查询之前——同特异性规则按源码顺序后者胜，若在其后会把 ≤768px 的 inline-flex
// 覆盖为不可见，汉堡按钮永远打不开。
const baseMenuToggleIdx = css.indexOf(".menu-toggle { display: none; }");
const mobileBreakpointIdx = css.indexOf("@media (max-width: 768px)");
assert.ok(baseMenuToggleIdx !== -1, "dist/css/style.css 应含基础 .menu-toggle{display:none}");
assert.ok(baseMenuToggleIdx < mobileBreakpointIdx,
  "CRIT-001: 基础 .menu-toggle{display:none} 必须位于 @media(max-width:768px) 之前");

// MED-001 回归（REVIEW-2026-08-15-TASK-030）：.nav-project 由 renderSidebar() 动态创建，
// 必须用侧栏容器事件委托（t.hasAttribute("data-nav") 沿祖先链）而非初始化时 querySelectorAll。
assert.ok(js.includes('t.hasAttribute("data-nav")'),
  "MED-001: 应存在侧栏事件委托（沿祖先链匹配 data-nav）");
assert.ok(!js.includes('querySelectorAll(".nav-item[data-nav], .nav-project")'),
  "MED-001: 不应再使用初始化时静态绑定 .nav-project（动态按钮绑不到）");

// ===== trend.js 纯函数单测（TASK-027）=====

function testCompletionRateSeries() {
  const pts = [
    { ts: 1000, summary: { total: 10, done: 2 } },
    { ts: 2000, summary: { total: 0, done: 0 } },       // total=0 → 空
    { ts: 3000 },                                        // 无 summary → 空
    { ts: 4000, summary: { total: 3, done: 2 } },        // 66.7%
  ];
  const s = AiTrend.completionRateSeries(pts);
  assert.strictEqual(s.length, 4);
  assert.deepStrictEqual(
    s.map(p => p.value),
    [20, null, null, 66.7],
    "完成率：done/total%，total=0/缺失 → null"
  );
  assert.strictEqual(s[0].ts, 1000, "完成率序列应保留 ts");
  assert.deepStrictEqual(AiTrend.completionRateSeries([]), [], "空输入 → 空序列");
  console.log("✓ completionRateSeries（done/total% + total=0 缺口）");
}

function testEventRateSeries() {
  // 1 小时窗口内 2 次迁移（open→done ×2）→ 1h 桶速率 2/h
  const pts = [
    { ts: 0, summary: { total: 5, open: 2, done: 3 } },
    { ts: 1800, summary: { total: 5, open: 1, done: 4 } },  // 1 迁移
    { ts: 3600, summary: { total: 5, open: 0, done: 5 } },  // 1 迁移
  ];
  const s = AiTrend.eventRateSeries(pts);
  assert.strictEqual(s.length, 1, "1h 窗口 → 1 个 1h 桶");
  assert.strictEqual(s[0].value, 2, "1h 桶内 2 次迁移 → 2/h");

  // 无变化 → 0/h（采样范围内无变化是有意义的值，不是缺口）
  const s2 = AiTrend.eventRateSeries([
    { ts: 0, summary: { total: 5, open: 2, done: 3 } },
    { ts: 3600, summary: { total: 5, open: 2, done: 3 } },
  ]);
  assert.strictEqual(s2[0].value, 0, "无变化 → 0/h");

  // 相邻 summary 缺失 → 该对不归桶（不按空对象计算假迁移）
  const s3 = AiTrend.eventRateSeries([
    { ts: 0, summary: { total: 5, open: 2, done: 3 } },
    { ts: 1800 },
    { ts: 3600, summary: { total: 5, open: 0, done: 5 } },
  ]);
  assert.strictEqual(s3[0].value, 0, "缺失 summary 对不归桶 → 0");

  // Δt=0 对不归桶；Δt 超 2 倍桶长（采样缺口）不归桶
  const s4 = AiTrend.eventRateSeries([
    { ts: 0, summary: { total: 5, open: 2, done: 3 } },
    { ts: 0, summary: { total: 5, open: 2, done: 3 } },        // Δt=0
    { ts: 10 * 3600, summary: { total: 5, open: 0, done: 5 } }, // 跨度 10h > 2h 桶长 → 缺口
  ]);
  assert.ok(s4.length >= 1 && s4.every(p => p.value === 0), "Δt=0 / 大跨度缺口 → 不归桶 → 0");

  // 多桶：2h 窗口 → 2 个 1h 桶，迁移按中点归入桶 0
  const s5 = AiTrend.eventRateSeries([
    { ts: 0, summary: { total: 5, open: 1, done: 4 } },
    { ts: 3600, summary: { total: 5, open: 0, done: 5 } },   // 1 迁移，中点 0.5h → 桶 0
    { ts: 7200, summary: { total: 5, open: 0, done: 5 } },
  ]);
  assert.strictEqual(s5.length, 2);
  assert.strictEqual(s5[0].value, 1, "桶 0 应含 1 迁移 → 1/h");
  assert.strictEqual(s5[1].value, 0, "桶 1 无迁移 → 0/h");

  assert.deepStrictEqual(AiTrend.eventRateSeries([]), [], "空输入 → 空");
  console.log("✓ eventRateSeries（分桶迁移次数/小时 + 缺失/Δt≤0/缺口不归桶）");
}

function testPickBucketHours() {
  assert.strictEqual(AiTrend.pickBucketHours(24), 1, "24h → 1h 桶（24 桶）");
  assert.strictEqual(AiTrend.pickBucketHours(168), 6, "7d → 6h 桶（28 桶）");
  assert.strictEqual(AiTrend.pickBucketHours(720), 24, "30d → 24h 桶（30 桶）");
  assert.strictEqual(AiTrend.pickBucketHours(2), 1, "短窗口回退 1h 桶");
  assert.strictEqual(AiTrend.pickBucketHours(0.5), 1, "亚小时窗口回退 1h 桶");
  console.log("✓ pickBucketHours（1/6/24h 档位 + 回退）");
}

function testDownsample() {
  const series = [1, 2, 3, 4, 5].map((v, i) => ({ ts: i, value: v }));
  assert.deepStrictEqual(AiTrend.downsample(series, 10), series, "n≤max 原样副本");
  const small = AiTrend.downsample(series, 3);
  assert.strictEqual(small.length, 3);
  assert.strictEqual(small[0].value, 1, "降采样保留首点");
  assert.strictEqual(small[small.length - 1].value, 5, "降采样保留末点");
  const empty = AiTrend.downsample([], 5);
  assert.deepStrictEqual(empty, [], "空输入 → 空");
  console.log("✓ downsample（保留首尾 + 上限）");
}

function testScaleSeries() {
  const series = [
    { ts: 0, value: 0 },
    { ts: 3600, value: null },   // 缺口
    { ts: 7200, value: 100 },
  ];
  const geom = AiTrend.scaleSeries(series, { width: 560, height: 160, yMin: 0, yMax: 100 });
  assert.strictEqual(geom.segments.length, 2, "null 应断开为 2 段");
  assert.strictEqual(geom.segments[0].length, 1);
  assert.strictEqual(geom.segments[1].length, 1);
  assert.strictEqual(geom.gridlines.length, 5, "5 条网格线");
  assert.ok(geom.xLabels.length >= 2, "时间标签 ≥2");
  // y 映射：value=0 → 底部（y=padT+innerH），value=100 → 顶部（y=padT）
  assert.ok(Math.abs(geom.y(0) - (geom.padT + geom.innerH)) < 1e-6, "y(0) 应落在底部");
  assert.ok(Math.abs(geom.y(100) - geom.padT) < 1e-6, "y(100) 应落在顶部");
  assert.ok(geom.x(0) < geom.x(2), "x 随时间递增");
  // 自动 y 上限（yMax null）：峰值 3 → nice ceiling 3
  const auto = AiTrend.scaleSeries([{ ts: 0, value: 3 }, { ts: 3600, value: 0 }],
                                   { width: 200, height: 100, yMin: 0, yMax: null });
  assert.strictEqual(auto.yMax, 3, "自动 y 上限 niceCeil(3) = 3");
  const auto2 = AiTrend.scaleSeries([{ ts: 0, value: 2.1 }, { ts: 3600, value: 0 }],
                                    { width: 200, height: 100, yMin: 0, yMax: null });
  assert.strictEqual(auto2.yMax, 3, "自动 y 上限 niceCeil(2.1) = 3");
  // 单点 → 居中不越界
  const single = AiTrend.scaleSeries([{ ts: 500, value: 50 }], { width: 200, height: 100, yMin: 0, yMax: 100 });
  assert.strictEqual(single.segments.length, 1);
  assert.ok(single.x(0) >= 0 && single.x(0) <= 200, "单点 x 应落在画布内");
  console.log("✓ scaleSeries（null 断开 + 网格/标签 + y 映射 + 自动上限 + 单点）");
}

function testFormat() {
  assert.strictEqual(AiTrend.formatY(20, "%"), "20%");
  assert.strictEqual(AiTrend.formatY(66.7, "%"), "66.7%");
  assert.strictEqual(AiTrend.formatY(0, "/h"), "0/h");
  assert.strictEqual(AiTrend.formatY(null, "%"), "");
  const d = new Date(Date.UTC(2026, 7, 15, 9, 5)); // 2026-08-15 09:05 UTC（本地时区格式化）
  assert.ok(/^\d{2}:\d{2}$/.test(AiTrend.formatTime(d.getTime() / 1000, "time")), "time 格式 HH:MM");
  assert.ok(/^\d{2}-\d{2}$/.test(AiTrend.formatTime(d.getTime() / 1000, "day")), "day 格式 MM-DD");
  assert.strictEqual(AiTrend.formatTime(null, "time"), "");
  console.log("✓ formatY / formatTime");
}

testCompletionRateSeries();
testEventRateSeries();
testPickBucketHours();
testDownsample();
testScaleSeries();
testFormat();

console.log("✓ dashboard.test: dist 产物完整且包含仪表盘关键元素（含趋势图）");

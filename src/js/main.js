// aimonitor — 仪表盘逻辑（零依赖原生 JS，TASK-021 现代化布局）
// 数据源: GET /api/status（契约见 docs/MONITOR-SPEC.md §4）；趋势图: GET /api/history（§4.2）
// 布局: 侧边栏导航 + 顶栏筛选 + 指标卡 + 告警横幅 + 项目表 + 任务列表 + 事件时间线 + 趋势图 + 详情抽屉
// 占位说明: 趋势图(TASK-027)、主题持久化(TASK-028)
(function () {
  "use strict";

  var state = {
    data: null,            // 最近一次 /api/status 响应（无筛选全量，下拉选项来源）
    currentProjectId: null,
    activeTaskId: null,    // 详情抽屉当前选中的任务
    activeNav: "overview", // 当前导航项
    theme: "dark",         // TASK-028: localStorage/prefers-color-scheme 持久化
    filters: { q: "", status: "", priority: "", assignee: "" },
    filteredTasks: null,   // TASK-025: 服务端筛选后的任务表格数据（null=无活动筛选）
    trend: null,           // TASK-027: 最近一次 /api/history 响应 {project, hours, points}
    pollMs: 30000,
    registration: null,    // TASK-055: 最近一次 /api/register/list 响应
    regAdminPwd: null,     // TASK-055: sessionStorage 中的 admin 密码
    regPollTimer: null,    // TASK-055: 注册列表轮询定时器
    regDetail: null,       // TASK-055: 当前打开的注册详情
    regActionPending: false, // TASK-056: 审批操作进行中
    regPendingAction: null,  // TASK-056: 待执行的操作 {action, reqId}
    // TASK-057: 注册码管理
    regCodes: null,          // 最近一次 /api/register/codes 响应
    regCodesTab: "queue",   // 当前子 tab：queue / codes
    regCodePendingRevoke: null, // 待吊销的 code
    regCodeGenPending: false, // TASK-057: 生成请求在途（F3 REVIEW：Enter 防重复）
    // TASK-073: Session 日志查看页（前端轮询，消息级）
    session: { projectId: null, summary: null, task: null, file: null, lines: null, timer: null }
  };

  var els = {
    select: document.getElementById("project-select"),
    sidebarProjects: document.getElementById("sidebar-projects"),
    sidebarFoot: document.getElementById("sidebar-foot"),
    navOverviewCount: document.getElementById("nav-overview-count"),
    navTasksCount: document.getElementById("nav-tasks-count"),
    navAlertCount: document.getElementById("nav-alert-count"),
    pageTitleSub: document.getElementById("page-title-sub"),
    pageSub: document.getElementById("page-sub"),
    rangeSeg: document.getElementById("range-seg"),
    refreshBtn: document.getElementById("refresh-btn"),
    themeToggle: document.getElementById("theme-toggle"),
    menuToggle: document.getElementById("menu-toggle"),
    sidebar: document.getElementById("sidebar"),
    sidebarMask: document.getElementById("sidebar-mask"),
    errorBanner: document.getElementById("error-banner"),
    alertBanner: document.getElementById("alert-banner"),
    overview: document.getElementById("overview"),
    projectCount: document.getElementById("project-count"),
    projectTableBody: document.querySelector("#project-table tbody"),
    taskBody: document.querySelector("#task-table tbody"),
    taskSearch: document.getElementById("task-search"),
    filterStatus: document.getElementById("filter-status"),
    filterPriority: document.getElementById("filter-priority"),
    filterAssignee: document.getElementById("filter-assignee"),
    events: document.getElementById("events"),
    eventsCount: document.getElementById("events-count"),
    trendCharts: document.getElementById("trend-charts"),
    trendEmpty: document.getElementById("trend-empty"),
    trendCompletion: document.getElementById("trend-completion"),
    trendEvent: document.getElementById("trend-event"),
    // TASK-073: Session 日志
    sessionTask: document.getElementById("session-task"),
    sessionFile: document.getElementById("session-file"),
    sessionMeta: document.getElementById("session-meta"),
    sessionFeed: document.getElementById("session-feed"),
    sessionLiveToggle: document.getElementById("session-live-toggle"),
    navSessionCount: document.getElementById("nav-session-count"),
    trendCompletionMeta: document.getElementById("trend-completion-meta"),
    trendEventMeta: document.getElementById("trend-event-meta"),
    focus: document.getElementById("focus"),
    statusBars: document.getElementById("status-bars"),
    statusLegend: document.getElementById("status-legend"),
    detailPanel: document.getElementById("detail-panel"),
    detailBody: document.getElementById("detail-body"),
    detailClose: document.getElementById("detail-close"),
    // TASK-055: 注册申请
    navRegCount: document.getElementById("nav-registration-count"),
    regPage: document.getElementById("registration-page"),
    regTable: document.querySelector("#registration-table tbody"),
    regEmpty: document.getElementById("registration-empty"),
    regCount: document.getElementById("registration-count"),
    regRefreshBtn: document.getElementById("reg-refresh-btn"),
    regDetailPanel: document.getElementById("reg-detail-panel"),
    regDetailBody: document.getElementById("reg-detail-body"),
    regDetailClose: document.getElementById("reg-detail-close"),
    adminPwdModal: document.getElementById("admin-pwd-modal"),
    adminPwdInput: document.getElementById("admin-pwd-input"),
    adminPwdConfirm: document.getElementById("admin-pwd-confirm"),
    adminPwdCancel: document.getElementById("admin-pwd-cancel"),
    adminPwdError: document.getElementById("admin-pwd-error"),
    // TASK-056: 审批操作
    regActionArea: document.getElementById("reg-action-area"),
    regConfirmModal: document.getElementById("reg-confirm-modal"),
    regConfirmTitle: document.getElementById("reg-confirm-title"),
    regConfirmMessage: document.getElementById("reg-confirm-message"),
    regConfirmRemark: document.getElementById("reg-confirm-remark"),
    regConfirmOk: document.getElementById("reg-confirm-ok"),
    regConfirmCancel: document.getElementById("reg-confirm-cancel"),
    regConfirmError: document.getElementById("reg-confirm-error"),
    regRejectModal: document.getElementById("reg-reject-modal"),
    regRejectReason: document.getElementById("reg-reject-reason"),
    regRejectOk: document.getElementById("reg-reject-ok"),
    regRejectCancel: document.getElementById("reg-reject-cancel"),
    regRejectError: document.getElementById("reg-reject-error"),
    // TASK-057: 注册码管理
    regTabs: document.querySelectorAll(".reg-tab"),
    regQueuePane: document.getElementById("reg-queue-pane"),
    regCodesPane: document.getElementById("reg-codes-pane"),
    regCodesTable: document.querySelector("#reg-codes-table tbody"),
    regCodesEmpty: document.getElementById("reg-codes-empty"),
    regCodesCount: document.getElementById("reg-codes-count"),
    regCodeGenerateBtn: document.getElementById("reg-code-generate-btn"),
    regCodeGenerateModal: document.getElementById("reg-code-generate-modal"),
    regCodeGenDesc: document.getElementById("reg-code-gen-desc"),
    regCodeGenDescErr: document.getElementById("reg-code-gen-desc-err"),
    regCodeGenProject: document.getElementById("reg-code-gen-project"),
    regCodeGenMaxUses: document.getElementById("reg-code-gen-max-uses"),
    regCodeGenMaxUsesErr: document.getElementById("reg-code-gen-max-uses-err"),
    regCodeGenExpire: document.getElementById("reg-code-gen-expire"),
    regCodeGenError: document.getElementById("reg-code-gen-error"),
    regCodeGenCancel: document.getElementById("reg-code-gen-cancel"),
    regCodeGenConfirm: document.getElementById("reg-code-gen-confirm"),
    regCodeResultModal: document.getElementById("reg-code-result-modal"),
    regCodeResultValue: document.getElementById("reg-code-result-value"),
    regCodeResultCopy: document.getElementById("reg-code-result-copy"),
    regCodeCopiedMsg: document.getElementById("reg-code-copied-msg"),
    regCodeResultClose: document.getElementById("reg-code-result-close"),
    regCodeRevokeModal: document.getElementById("reg-code-revoke-modal"),
    regCodeRevokeMessage: document.getElementById("reg-code-revoke-message"),
    regCodeRevokeError: document.getElementById("reg-code-revoke-error"),
    regCodeRevokeCancel: document.getElementById("reg-code-revoke-cancel"),
    regCodeRevokeConfirm: document.getElementById("reg-code-revoke-confirm")
  };

  var STATUS_ORDER = ["open", "in-progress", "in-review", "blocked", "done", "cancelled"];
  var STATUS_LABEL = { "open": "open", "in-progress": "进行中", "in-review": "审查中", "blocked": "阻塞", "done": "完成", "cancelled": "取消" };
  var RANGE_HOURS = { "24h": 24, "7d": 168, "30d": 720 }; // TASK-027: range-seg → /api/history hours

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function badge(text, cls, label) {
    var b = el("span", "badge " + (cls || ""));
    b.textContent = label || text;
    return b;
  }

  // —— 错误横幅（API 请求失败；保留上次数据）——
  function showError(msg) {
    els.errorBanner.innerHTML = "";
    els.errorBanner.appendChild(el("span", "ab-icon", "⚠"));
    els.errorBanner.appendChild(el("span", "ab-body", msg + "（保留上次数据）"));
    els.errorBanner.classList.remove("hidden");
  }

  function clearError() {
    els.errorBanner.classList.add("hidden");
  }

  // —— 项目/心跳辅助 ——
  function projectInData(data, id) {
    if (!data) return null;
    for (var i = 0; i < data.projects.length; i++) {
      if (data.projects[i].id === id) return data.projects[i];
    }
    return data.projects[0] || null;
  }

  function projectById(id) {
    return projectInData(state.data, id);
  }

  function heartbeatStatus(hb, threshold) {
    if (!hb || !hb.exists) return { cls: "heartbeat-off", label: "无进程" };
    if (hb.age_seconds === null || hb.age_seconds > threshold) return { cls: "heartbeat-stale", label: "卡死" };
    return { cls: "heartbeat-ok", label: "存活" };
  }

  function ago(seconds) {
    if (seconds === null || seconds === undefined) return "";
    if (seconds < 60) return Math.round(seconds) + "s 前";
    if (seconds < 3600) return Math.round(seconds / 60) + "m 前";
    return Math.round(seconds / 3600) + "h 前";
  }

  // —— 告警（TASK-026：服务端派生 alerts 字段，前端直接消费）——
  function deriveAlerts() {
    if (!state.data || !state.data.alerts) return [];
    return state.data.alerts.items || [];
  }

  function renderAlertBanner(alerts) {
    els.alertBanner.innerHTML = "";
    if (alerts.length === 0) {
      els.alertBanner.classList.add("hidden");
    } else {
      els.alertBanner.classList.remove("hidden");
      els.alertBanner.appendChild(el("span", "ab-icon", "▲"));
      var body = el("span", "ab-body");
      body.appendChild(el("b", null, alerts[0].project + " "));
      body.appendChild(document.createTextNode(alerts[0].text));
      if (alerts.length > 1) body.appendChild(el("span", "muted", " 等 " + alerts.length + " 条"));
      els.alertBanner.appendChild(body);
      els.alertBanner.appendChild(el("span", "ab-count", alerts.length + " 条告警"));
    }
    // 导航告警计数
    els.navAlertCount.textContent = alerts.length ? String(alerts.length) : "";
    els.navAlertCount.classList.toggle("alert", alerts.length > 0);
  }

  // —— 侧边栏 ——
  function renderSidebar(alerts) {
    var threshold = state.data.heartbeat_stale_threshold_seconds || 300;
    els.navOverviewCount.textContent = String(state.data.projects.length);
    var current = projectById(state.currentProjectId);
    els.navTasksCount.textContent = current ? String((current.summary || {}).total || 0) : "0";

    // 项目列表
    els.sidebarProjects.innerHTML = "";
    state.data.projects.forEach(function (p) {
      var btn = el("button", "nav-project" + (p.id === state.currentProjectId ? " active" : ""));
      btn.setAttribute("data-project", p.id);
      // TASK-043: 实例级展示——同 group 多实例用实例 id 区分（按钮文字同名不混淆）；
      // title 带实例/离线详情
      var label = p.name;
      if (p.group && p.group !== p.id) label += " (" + p.id + ")";
      btn.title = label + (p.error ? " — " + p.error : "");
      var dot = el("span", "dot");
      var hbC = heartbeatStatus(p.heartbeat && p.heartbeat.coder, threshold);
      dot.style.color = p.error ? "var(--err)" : (hbC.cls === "heartbeat-ok" ? "var(--ok)" : hbC.cls === "heartbeat-stale" ? "var(--warn)" : "var(--text-faint)");
      btn.appendChild(dot);
      btn.appendChild(el("span", "pname", label));
      if (p.error) btn.appendChild(el("span", "perr", "⚠"));
      btn.addEventListener("click", function () { setCurrentProject(p.id); });
      els.sidebarProjects.appendChild(btn);
    });

    // 底部状态
    els.sidebarFoot.innerHTML = "";
    var online = state.data.projects.filter(function (p) { return !p.error; }).length;
    els.sidebarFoot.appendChild(document.createTextNode(
      online + "/" + state.data.projects.length + " 项目在线 · 轮询 " + (state.data.poll_interval_seconds || 30) + "s"));
    var health = el("div", "health-pill" + (alerts.length ? " bad" : ""));
    health.appendChild(el("span", "dot"));
    health.appendChild(document.createTextNode(alerts.length ? "存在告警" : "系统运行正常"));
    els.sidebarFoot.appendChild(health);
  }

  function renderProjectSelect() {
    els.select.innerHTML = "";
    if (!state.data) return;
    state.data.projects.forEach(function (p) {
      var o = document.createElement("option");
      o.value = p.id;
      // TASK-043: 实例级展示——同 group 多实例（group != id）追加实例 id 区分（同名实例
      // 不混淆）；agent 项目标记 [agent]；离线追加 error 文案（含 agent 离线）
      var label = p.name;
      if (p.group && p.group !== p.id) label += " (" + p.id + ")";
      if (p.transport === "agent") label += " [agent]";
      if (p.error) label += " ⚠ " + p.error;
      o.textContent = label;
      if (p.id === state.currentProjectId) o.selected = true;
      els.select.appendChild(o);
    });
  }

  function setCurrentProject(id) {
    state.currentProjectId = id;
    state.activeTaskId = null;
    state.filteredTasks = null;
    if (els.select) els.select.value = id;
    els.detailPanel.classList.add("hidden");
    render();
    refreshTasks(); // 有活动筛选时对新项目重新应用
    loadTrend(true); // TASK-027: 切换项目时刷新趋势图
  }

  // —— 顶栏 ——
  function renderTopbar() {
    var p = projectById(state.currentProjectId);
    // TASK-043: 实例级展示——同 group 多实例追加实例 id；agent 项目标记传输方式
    els.pageTitleSub.textContent = p ? "· " + p.name +
      (p.group && p.group !== p.id ? " (" + p.id + ")" : "") +
      (p.transport === "agent" ? " · agent" : "") : "";
    var d = new Date(state.data.generated_at * 1000);
    var ts = d.toLocaleTimeString();
    var threshold = Math.round((state.data.heartbeat_stale_threshold_seconds || 300) / 60);
    els.pageSub.textContent = "更新于 " + ts + " · 自动刷新 " + (state.data.poll_interval_seconds || 30) + "s · 心跳阈值 " + threshold + "min";
  }

  function bindRangeSeg() {
    els.rangeSeg.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-range]");
      if (!btn) return;
      var all = els.rangeSeg.querySelectorAll("button");
      for (var i = 0; i < all.length; i++) all[i].classList.remove("active");
      btn.classList.add("active");
      loadTrend(); // TASK-027: 按范围重新拉取 /api/history
    });
  }

  // —— 趋势图（TASK-027：/api/history → 手写 SVG 折线图，纯函数见 src/js/trend.js）——

  function activeRangeHours() {
    var btn = els.rangeSeg.querySelector("button.active");
    var key = btn && btn.getAttribute("data-range");
    return RANGE_HOURS[key] || 24;
  }

  function showTrendLoading() {
    els.trendCharts.classList.remove("hidden");
    els.trendEmpty.classList.add("hidden");
    els.trendCompletion.innerHTML = "";
    els.trendCompletion.appendChild(el("div", "trend-placeholder", "加载中…"));
    els.trendEvent.innerHTML = "";
    els.trendEvent.appendChild(el("div", "trend-placeholder", "加载中…"));
    els.trendCompletionMeta.textContent = "";
    els.trendEventMeta.textContent = "";
  }

  function renderTrendEmpty(msg) {
    els.trendCharts.classList.add("hidden");
    els.trendEmpty.classList.remove("hidden");
    els.trendEmpty.textContent = msg || "暂无历史数据（等待轮询采样）";
  }

  // 拉取当前项目 + 当前范围的 /api/history；带 currentProjectId 防串台校验；失败保留旧图
  function loadTrend(force) {
    var p = projectById(state.currentProjectId);
    if (!p) return;
    var projectId = p.id;
    var hours = activeRangeHours();
    if (!force && state.trend && state.trend.project === projectId && state.trend.hours === hours) {
      renderTrend(); // 已有同范围数据，直接渲染
      return;
    }
    if (!state.trend || state.trend.project !== projectId) showTrendLoading();
    fetch("/api/history?project=" + encodeURIComponent(projectId) + "&hours=" + hours, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (state.currentProjectId !== projectId) return; // 项目已切换，丢弃过期响应
        state.trend = { project: projectId, hours: hours, points: data.points || [] };
        renderTrend();
      })
      .catch(function (e) {
        if (state.currentProjectId !== projectId) return;
        // 已有数据则保留旧图（错误横幅已提示）；无旧数据才显示错误占位
        if (!state.trend || state.trend.project !== projectId) {
          renderTrendEmpty("趋势加载失败: " + e.message);
        }
      });
  }

  function formatCountMeta(series, unit) {
    var n = series.length;
    if (!n) return "无数据";
    var vals = series.filter(function (p) { return p.value !== null && p.value !== undefined; });
    if (!vals.length) return n + " 点";
    var last = vals[vals.length - 1].value;
    var peak = 0;
    vals.forEach(function (p) { if (p.value > peak) peak = p.value; });
    var T = window.AiTrend || {};
    return n + " 点 · 最新 " + (T.formatY ? T.formatY(last, unit) : last) +
           " · 峰值 " + (T.formatY ? T.formatY(peak, unit) : peak);
  }

  function renderTrend() {
    var p = projectById(state.currentProjectId);
    var t = state.trend;
    if (!p || !t || t.project !== p.id) { renderTrendEmpty("加载趋势数据…"); return; }
    var points = t.points || [];
    if (!points.length) { renderTrendEmpty("该项目暂无历史数据（等待轮询采样）"); return; }
    var T = window.AiTrend;
    if (!T) { renderTrendEmpty("趋势模块（trend.js）未加载"); return; }

    els.trendCharts.classList.remove("hidden");
    els.trendEmpty.classList.add("hidden");
    var timeMode = t.hours <= 24 ? "time" : "day";

    var comp = T.downsample(T.completionRateSeries(points), 400);
    els.trendCompletionMeta.textContent = formatCountMeta(comp, "%");
    renderLineChart(els.trendCompletion, comp, {
      width: 560, height: 160, yMin: 0, yMax: 100,
      unit: "%", timeMode: timeMode, area: true, accent: "accent",
      emptyText: "无有效采样（任务 total 均为 0）"
    });

    var ev = T.downsample(T.eventRateSeries(points), 400);
    els.trendEventMeta.textContent = formatCountMeta(ev, "/h");
    renderLineChart(els.trendEvent, ev, {
      width: 560, height: 160, yMin: 0, yMax: null,
      unit: "/h", timeMode: timeMode, area: true, accent: "accent-2",
      emptyText: "无有效采样（无相邻快照可计算速率）"
    });
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs) {
    var n = document.createElementNS(SVG_NS, tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) n.setAttribute(k, attrs[k]);
    }
    return n;
  }

  // 用 trend.js 几何结果构建 SVG：网格线 / 时间标签 / 折线（缺口断开）/ 面积 / 数据点 tooltip
  function renderLineChart(mount, series, opts) {
    mount.innerHTML = "";
    var T = window.AiTrend;
    var hasValue = series.some(function (p) { return p.value !== null && p.value !== undefined; });
    if (!hasValue) {
      mount.appendChild(el("div", "trend-placeholder", opts.emptyText || "暂无数据"));
      return;
    }
    var geom = T.scaleSeries(series, opts);
    var is2 = opts.accent === "accent-2";
    var svg = svgEl("svg", {
      viewBox: "0 0 " + geom.width + " " + geom.height,
      "class": "trend-svg-el",
      role: "img"
    });

    geom.gridlines.forEach(function (g) {
      svg.appendChild(svgEl("line", {
        x1: geom.padL, x2: geom.width - geom.padR, y1: g.y, y2: g.y, "class": "trend-grid"
      }));
      var tx = svgEl("text", { x: geom.width - geom.padR + 4, y: g.y + 3, "class": "trend-y" });
      tx.textContent = T.formatY(g.value, opts.unit);
      svg.appendChild(tx);
    });

    geom.xLabels.forEach(function (l) {
      var tx = svgEl("text", { x: l.x, y: geom.height - 6, "class": "trend-x", "text-anchor": "middle" });
      tx.textContent = T.formatTime(l.ts, opts.timeMode);
      svg.appendChild(tx);
    });

    geom.segments.forEach(function (seg) {
      if (!seg.length) return;
      if (seg.length >= 2) {
        var pts = seg.map(function (pt) { return pt.x.toFixed(1) + "," + pt.y.toFixed(1); }).join(" ");
        svg.appendChild(svgEl("polyline", { points: pts, "class": "trend-line" + (is2 ? " is2" : "") }));
        if (opts.area) {
          var base = (geom.height - geom.padB).toFixed(1);
          var areaPts = pts + " " + seg[seg.length - 1].x.toFixed(1) + "," + base +
                        " " + seg[0].x.toFixed(1) + "," + base;
          svg.appendChild(svgEl("polygon", { points: areaPts, "class": "trend-area" + (is2 ? " is2" : "") }));
        }
      }
      if (series.length <= 120) {
        seg.forEach(function (pt) {
          var dot = svgEl("circle", {
            cx: pt.x.toFixed(1), cy: pt.y.toFixed(1), r: 2.5,
            "class": "trend-dot" + (is2 ? " is2" : "")
          });
          var tip = svgEl("title", null);
          tip.textContent = T.formatTime(pt.ts, opts.timeMode) + " · " + T.formatY(pt.value, opts.unit);
          dot.appendChild(tip);
          svg.appendChild(dot);
        });
      }
    });

    mount.appendChild(svg);
  }

  // —— 主题（TASK-028：localStorage 记忆 + prefers-color-scheme 默认 + 切换持久化）——
  function getSavedTheme() {
    try {
      var saved = localStorage.getItem("aimonitor-theme");
      return saved === "light" || saved === "dark" ? saved : null;
    } catch (e) { return null; } // localStorage 不可用（隐私模式等）时回退默认
  }
  function systemTheme() {
    try {
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    } catch (e) { return "dark"; }
  }
  function initTheme() {
    setTheme(getSavedTheme() || systemTheme()); // 用户记忆优先，其次系统偏好，默认 dark
  }
  function setTheme(t) {
    state.theme = t === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", state.theme);
    els.themeToggle.textContent = state.theme === "dark" ? "☀️" : "🌙";
    try { localStorage.setItem("aimonitor-theme", state.theme); } catch (e) { /* 不可用时忽略 */ }
  }

  // —— 指标卡（当前项目）——
  function renderOverview(p) {
    var s = p.summary || {};
    var total = s.total || 0;
    var done = s.done || 0;
    var doneRate = total ? Math.round(done / total * 100) : 0;
    var threshold = state.data.heartbeat_stale_threshold_seconds || 300;
    var hbC = heartbeatStatus(p.heartbeat && p.heartbeat.coder, threshold);

    els.overview.innerHTML = "";
    var cards = [];

    // 总任务（done/total）
    var c0 = el("div", "card");
    var n0 = el("div", "num");
    n0.appendChild(document.createTextNode(String(done)));
    n0.appendChild(el("small", null, "/" + total));
    c0.appendChild(n0);
    c0.appendChild(el("div", "lbl", "总任务"));
    cards.push(c0);

    // 完成率
    var c1 = el("div", "card");
    c1.appendChild(el("div", "num", doneRate + "%"));
    c1.appendChild(el("div", "lbl", "完成率"));
    cards.push(c1);

    // 进行中
    var c2 = el("div", "card");
    var n2 = el("div", "num info", String(s["in-progress"] || 0));
    c2.appendChild(n2);
    c2.appendChild(el("div", "lbl", "进行中"));
    cards.push(c2);

    // 审查中
    var c3 = el("div", "card");
    var n3 = el("div", "num", String(s["in-review"] || 0));
    n3.style.color = "var(--accent-2)";
    c3.appendChild(n3);
    c3.appendChild(el("div", "lbl", "审查中"));
    cards.push(c3);

    // 阻塞
    var c4 = el("div", "card");
    var n4 = el("div", "num err", String(s["blocked"] || 0));
    c4.appendChild(n4);
    c4.appendChild(el("div", "lbl", "阻塞"));
    if ((s["blocked"] || 0) > 0) c4.appendChild(el("div", "trend down", "⚠ 需处理"));
    cards.push(c4);

    // Coder 心跳
    var c5 = el("div", "card");
    var hbNumCls = hbC.cls === "heartbeat-ok" ? "ok" : (hbC.cls === "heartbeat-stale" ? "err" : "");
    var n5 = el("div", "num" + (hbNumCls ? " " + hbNumCls : ""), hbC.label);
    c5.appendChild(n5);
    c5.appendChild(el("div", "lbl", "Coder 心跳"));
    if (p.heartbeat && p.heartbeat.coder && p.heartbeat.coder.exists) {
      c5.appendChild(el("div", "trend" + (hbC.label === "卡死" ? " down" : ""), "上次 " + ago(p.heartbeat.coder.age_seconds)));
    }
    cards.push(c5);

    cards.forEach(function (c) { els.overview.appendChild(c); });
  }

  // —— 项目总览表 ——
  function renderProjectTable() {
    if (!state.data) return;
    var threshold = state.data.heartbeat_stale_threshold_seconds || 300;
    els.projectTableBody.innerHTML = "";
    els.projectCount.textContent = state.data.projects.length + " 个项目";
    state.data.projects.forEach(function (p) {
      var s = p.summary || {};
      var total = s.total || 0;
      var done = s.done || 0;
      var doneRate = total ? Math.round(done / total * 100) : 0;

      var tr = document.createElement("tr");
      tr.className = "project-row" + (p.id === state.currentProjectId ? " active" : "");
      tr.setAttribute("data-project-id", p.id);
      tr.title = "点击切换当前项目";
      tr.addEventListener("click", function () { setCurrentProject(p.id); });

      var tdName = el("td");
      var nameWrap = el("span", "ov-project");
      // TASK-043: 实例级展示——同 group 多实例追加实例 id（与侧栏/选择器/顶栏一致；
      // 同 group 同 name 实例行可区分，MED-001 返工）；agent 项目标记 transport 徽章
      // （离线 error 徽章保留，展示 TASK-039 服务端派生的 error='agent 离线' 上下文）
      nameWrap.textContent = p.name + (p.group && p.group !== p.id ? " (" + p.id + ")" : "");
      tdName.appendChild(nameWrap);
      // TASK-043: 实例级展示——同 group 多实例用实例组徽章区分（一行一实例，同组多行可见）；
      // agent 项目标记 transport 徽章（离线 error 徽章保留，展示 TASK-039 服务端派生的
      // error='agent 离线' 上下文）
      if (p.group && p.group !== p.id) {
        var groupBadge = badge(p.group, "ov-group", p.group);
        groupBadge.title = "实例组: " + p.group;
        tdName.appendChild(groupBadge);
      }
      if (p.transport === "agent") {
        var transBadge = badge(p.transport, "ov-agent", "agent");
        transBadge.title = "agent 推送（远端机器）";
        tdName.appendChild(transBadge);
      }
      if (p.error) {
        var errBadge = badge("⚠ " + p.error, "st-blocked", "⚠");
        errBadge.title = p.error;
        tdName.appendChild(errBadge);
      }
      tr.appendChild(tdName);

      tr.appendChild(el("td", null, String(total)));
      tr.appendChild(el("td", null, String(done)));

      var tdRate = el("td");
      var bar = el("div", "ov-bar");
      var fill = el("div", "ov-bar-fill");
      fill.style.width = doneRate + "%";
      bar.appendChild(fill);
      tdRate.appendChild(bar);
      tdRate.appendChild(el("span", "ov-rate", doneRate + "%"));
      tr.appendChild(tdRate);

      ["open", "in-progress", "in-review", "blocked"].forEach(function (st) {
        var n = s[st] || 0;
        var td = el("td");
        if (n > 0) td.appendChild(badge(n, "st-" + st, String(n)));
        else td.appendChild(el("span", "ov-zero", "0"));
        tr.appendChild(td);
      });

      // 告警列（TASK-026：projects[].alerts 服务端派生，悬浮显示条目明细）
      var pAlerts = p.alerts || [];
      var tdAlert = el("td");
      if (pAlerts.length) {
        var ab = badge(pAlerts.length, "st-alert", String(pAlerts.length) + " 条");
        ab.title = pAlerts.map(function (a) { return "[" + a.level + "] " + a.text; }).join("\n");
        tdAlert.appendChild(ab);
      } else {
        tdAlert.appendChild(el("span", "ov-zero", "0"));
      }
      tr.appendChild(tdAlert);

      var tdHb = el("td");
      var hbC = heartbeatStatus(p.heartbeat && p.heartbeat.coder, threshold);
      var hbR = heartbeatStatus(p.heartbeat && p.heartbeat.reviewer, threshold);
      if (p.error) {
        tdHb.appendChild(el("span", "heartbeat-off", "—"));
      } else {
        tdHb.appendChild(el("span", hbC.cls, "C:" + hbC.label));
        tdHb.appendChild(el("span", "ov-hb-sep", " / "));
        tdHb.appendChild(el("span", hbR.cls, "R:" + hbR.label));
      }
      tr.appendChild(tdHb);

      tr.appendChild(el("td", null, String(p.verification_count || 0)));
      tr.appendChild(el("td", null, String(p.review_count || 0)));

      els.projectTableBody.appendChild(tr);
    });
  }

  // —— 任务列表（TASK-025 后端化：筛选/搜索由 /api/status 服务端执行）——
  function filterQuery() {
    var parts = [];
    if (state.filters.q) parts.push("q=" + encodeURIComponent(state.filters.q));
    if (state.filters.status) parts.push("status=" + encodeURIComponent(state.filters.status));
    if (state.filters.priority) parts.push("priority=" + encodeURIComponent(state.filters.priority));
    if (state.filters.assignee) parts.push("assignee=" + encodeURIComponent(state.filters.assignee));
    return parts.length ? "?" + parts.join("&") : "";
  }

  function hasActiveFilters() {
    return !!(state.filters.q || state.filters.status || state.filters.priority || state.filters.assignee);
  }

  // 按当前筛选重新请求 /api/status → 只更新任务表格（下拉选项仍取自全量 state.data）
  function refreshTasks() {
    var p = projectById(state.currentProjectId);
    if (!p) return;
    if (!hasActiveFilters()) {
      state.filteredTasks = null;
      renderTasks(p);
      return;
    }
    fetch("/api/status" + filterQuery(), { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (state.currentProjectId !== p.id) return; // 项目已切换，丢弃过期响应
        var fp = projectInData(data, p.id);
        state.filteredTasks = (fp && fp.tasks) || [];
        renderTasks(projectById(state.currentProjectId));
      })
      .catch(function (e) {
        showError("筛选加载失败: " + e.message);
      });
  }

  function fillFilterOptions(select, values, labelAll) {
    var current = select.value;
    select.innerHTML = "";
    var all = document.createElement("option");
    all.value = "";
    all.textContent = labelAll;
    select.appendChild(all);
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v;
      o.textContent = v;
      select.appendChild(o);
    });
    select.value = current; // 保留用户选择
  }

  function renderTasks(p) {
    var allTasks = p.tasks || [];
    var visible = (state.filteredTasks !== null && state.currentProjectId === p.id) ? state.filteredTasks : allTasks;

    // 筛选下拉选项来自全量任务（保持选项完整，避免服务端筛选后下拉被截断）
    var statuses = [], priorities = [], assignees = [];
    allTasks.forEach(function (t) {
      if (t.status && statuses.indexOf(t.status) === -1) statuses.push(t.status);
      if (t.priority && priorities.indexOf(t.priority) === -1) priorities.push(t.priority);
      var a = t.assignee || "";
      if (a && assignees.indexOf(a) === -1) assignees.push(a);
    });
    fillFilterOptions(els.filterStatus, statuses, "全部状态");
    fillFilterOptions(els.filterPriority, priorities, "全部优先级");
    fillFilterOptions(els.filterAssignee, assignees, "全部 assignee");

    els.taskBody.innerHTML = "";
    visible.forEach(function (t) {
      var tr = document.createElement("tr");
      tr.className = "task-row" + (state.activeTaskId === t.id ? " active" : "");
      tr.setAttribute("data-task-id", t.id);
      tr.title = "点击查看详情";
      tr.addEventListener("click", function () { openTaskDetail(t); });
      var tdName = el("td");
      tdName.appendChild(el("span", "task-name", t.id + " " + (t.description || t.name)));
      tr.appendChild(tdName);
      var tdStatus = el("td");
      tdStatus.appendChild(badge(t.status, "st-" + t.status, STATUS_LABEL[t.status] || t.status));
      tr.appendChild(tdStatus);
      var tdPrio = el("td");
      tdPrio.appendChild(badge(t.priority, "st-" + t.priority, t.priority));
      tr.appendChild(tdPrio);
      var tdRisk = el("td");
      tdRisk.appendChild(badge(t.risk, "st-" + t.risk, t.risk));
      tr.appendChild(tdRisk);
      tr.appendChild(el("td", null, t.assignee || ""));
      tr.appendChild(el("td", null, t.updated || ""));
      els.taskBody.appendChild(tr);
    });
    if (!visible.length) {
      var trEmpty = el("tr");
      trEmpty.appendChild(el("td", "muted", "无匹配任务"));
      els.taskBody.appendChild(trEmpty);
    }
  }

  // —— 事件时间线（TASK-023：改用 /api/projects/:id/events 完整时间线 API）——
  function formatEventTime(ts) {
    if (!ts) return "";
    var d = (typeof ts === "number") ? new Date(ts * 1000) : new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    function p(n) { return (n < 10 ? "0" : "") + n; }
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function outcomeDotClass(outcome) {
    if (outcome === "ok") return "ok";
    if (outcome === "no_task") return "no_task";
    if (outcome === "err" || outcome === "error" || outcome === "timeout" || outcome === "blocked_p0") return "err";
    return "info";
  }

  function renderEvents(p) {
    els.eventsCount.textContent = "";
    els.events.innerHTML = "";
    els.events.appendChild(el("div", "muted", "加载事件时间线…"));
    var projectId = p.id;
    fetch("/api/projects/" + encodeURIComponent(projectId) + "/events?limit=10", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (state.currentProjectId !== projectId) return; // 项目已切换，丢弃过期响应
        populateEvents(data);
      })
      .catch(function () {
        if (state.currentProjectId !== projectId) return;
        els.eventsCount.textContent = "";
        els.events.innerHTML = "";
        els.events.appendChild(el("div", "muted", "事件时间线加载失败"));
      });
  }

  function eventTimeSortKey(it) {
    var ts = it.ev && it.ev.ts;
    if (typeof ts === "number") return ts;
    var d = Date.parse(ts);
    return isNaN(d) ? 0 : d / 1000;
  }

  function populateEvents(data) {
    var names = ["coder", "reviewer"];
    var items = [];
    var total = 0;
    names.forEach(function (who) {
      var counts = data.counts || {};
      total += counts[who] || 0;
      (data.events && data.events[who] ? data.events[who] : []).forEach(function (ev) {
        items.push({ who: who, ev: ev });
      });
    });
    // TASK-071：task 事件流（seq/cursor 确认）并入同一时间线展示
    var te = data.task_events || { count: 0, cursor: null, events: [] };
    total += te.count || 0;
    (te.events || []).forEach(function (ev) {
      items.push({ who: "task", ev: ev });
    });
    items.sort(function (a, b) { return eventTimeSortKey(b) - eventTimeSortKey(a); });

    els.eventsCount.textContent = "最近 " + items.length + " / " + total;
    els.events.innerHTML = "";
    if (!items.length) {
      els.events.appendChild(el("div", "muted", "暂无事件（无 autoloop 循环运行）"));
      return;
    }
    items.forEach(function (it) {
      var item = el("div", "tl-item");
      item.appendChild(el("span", "tl-dot " + outcomeDotClass(it.ev.outcome)));
      var body = el("div", "tl-body");
      var time = el("div", "tl-time", formatEventTime(it.ev.ts) + " · " + it.who);
      body.appendChild(time);
      var taskText;
      if (it.who === "task") {
        taskText = (it.ev.task ? it.ev.task + " " : "") + (it.ev.ev || it.ev.outcome || "?");
        if (typeof it.ev.seq === "number") taskText += "  #" + it.ev.seq;
      } else {
        taskText = (it.ev.task && it.ev.task !== "-" ? it.ev.task + " → " : "") + (it.ev.outcome || "?");
      }
      body.appendChild(el("div", "tl-task", taskText));
      item.appendChild(body);
      els.events.appendChild(item);
    });
  }

  // —— 当前焦点 ——
  function renderFocus(p) {
    var f = p.focus || {};
    els.focus.innerHTML = "";
    if (f.current) {
      els.focus.appendChild(el("div", "fc-label", "当前"));
      els.focus.appendChild(el("div", null, f.current));
    }
    if (f.next) {
      els.focus.appendChild(el("div", "fc-label fc-block", "下一步"));
      els.focus.appendChild(el("div", null, f.next));
    }
    if (!f.current && !f.next) {
      els.focus.appendChild(el("div", "fc-empty", "（无焦点数据）"));
    }
  }

  // —— 状态分布 ——
  function renderStatusBars(p) {
    var s = p.summary || {};
    var total = s.total || 0;
    els.statusBars.innerHTML = "";
    els.statusLegend.innerHTML = "";
    if (total === 0) {
      els.statusBars.appendChild(el("div", "muted", "无任务"));
      return;
    }
    STATUS_ORDER.forEach(function (st) {
      var n = s[st] || 0;
      if (n > 0) {
        var seg = el("div", "seg st-" + st);
        seg.style.width = (n / total * 100) + "%";
        seg.title = STATUS_LABEL[st] + ": " + n;
        els.statusBars.appendChild(seg);
      }
      var it = el("span", "it");
      it.appendChild(el("span", "dot st-" + st));
      it.appendChild(document.createTextNode(STATUS_LABEL[st] + " " + (s[st] || 0)));
      els.statusLegend.appendChild(it);
    });
  }

  // —— 注册申请页面（TASK-055）——

  // host_info 双格式解析：JSON（{hostname, ip, ...}）或 "key:value, key:value" 明文
  // （MONITOR-SPEC §3.2.6 请求示例 "hostname:dev-box, ip:192.168.1.5"）
  function parseHostInfo(raw) {
    var out = {};
    if (!raw) return out;
    if (typeof raw === "object") return raw;
    // 先尝试 JSON
    try {
      var obj = JSON.parse(raw);
      if (obj && typeof obj === "object") return obj;
    } catch (e) { /* 非 JSON，走明文解析 */ }
    // 明文 key:value, key:value（兼容缺失部分字段/单 key）
    String(raw).split(",").forEach(function (part) {
      var idx = part.indexOf(":");
      if (idx < 0) return;
      var k = part.slice(0, idx).trim().toLowerCase();
      var v = part.slice(idx + 1).trim();
      if (k) out[k] = v;
    });
    return out;
  }

  function regStatusLabel(status) {
    var map = {
      "pending": "⏳ 待审批",
      "approved": "✅ 已批准",
      "rejected": "❌ 已拒绝",
      "expired": "⏳ 已过期",
      "revoked": "🔒 已吊销"
    };
    return map[status] || status;
  }

  function regTagLabel(row) {
    if (row.enrollment_code) return { cls: "preauth", label: "🔵 预授权" };
    return { cls: "blind", label: "🟡 盲申请" };
  }

  function renderRegistrationPage() {
    var data = state.registration;
    if (!data) {
      els.regPage.classList.add("hidden");
      return;
    }
    els.regPage.classList.remove("hidden");

    var pending = 0;
    data.forEach(function (r) { if (r.status === "pending") pending++; });

    // 更新计数
    els.regCount.textContent = data.length + " 条申请" + (pending ? " · " + pending + " 待审批" : "");
    els.navRegCount.textContent = pending ? String(pending) : "";

    // 空态：清空表格并显示占位
    if (!data.length) {
      els.regEmpty.classList.remove("hidden");
      els.regTable.innerHTML = "";
      return;
    }
    els.regEmpty.classList.add("hidden");

    // 行级 diff：仅新增/删除/有变化的行更新，避免整表 innerHTML 重建闪烁（UI-004 不闪烁）
    var tbody = els.regTable;
    var existing = {};
    var rows = tbody.querySelectorAll("tr[data-req-id]");
    for (var i = 0; i < rows.length; i++) {
      existing[rows[i].getAttribute("data-req-id")] = rows[i];
    }

    data.forEach(function (row) {
      // 解析 host_info（JSON 或 "key:value, key:value"）
      var hostInfo = parseHostInfo(row.host_info);
      var hostname = hostInfo.hostname || hostInfo.host || "—";
      var ip = hostInfo.ip || hostInfo.ip_address || "—";

      // 时间
      var ts = row.created_at ? new Date(row.created_at * 1000).toLocaleString() : "—";

      // 状态标记
      var tag = regTagLabel(row);

      // 状态 badge
      var statusLabel = regStatusLabel(row.status);

      // UI-003：被拒绝的申请在列表状态列展示拒绝原因
      var reasonHtml = (row.status === "rejected" && row.reject_reason)
        ? "<div class=\"reg-list-reason\">原因：" + escapeHtml(row.reject_reason) + "</div>"
        : "";

      var html =
        "<td>" + escapeHtml(row.project_id) + "</td>" +
        "<td>" + escapeHtml(hostname) + "</td>" +
        "<td>" + escapeHtml(ip) + "</td>" +
        "<td>" + ts + "</td>" +
        "<td><span class=\"status-badge " + row.status + "\">" + statusLabel + "</span>" + reasonHtml + "</td>" +
        "<td><span class=\"tag-badge " + tag.cls + "\">" + tag.label + "</span></td>";

      var tr = existing[row.req_id];
      if (tr) {
        // 内容有变化才更新（避免无谓重绘闪烁）
        if (tr.getAttribute("data-sign") !== html) {
          tr.innerHTML = html;
          tr.setAttribute("data-sign", html);
        }
        delete existing[row.req_id];
      } else {
        tr = document.createElement("tr");
        tr.setAttribute("data-req-id", row.req_id);
        tr.setAttribute("data-sign", html);
        tr.style.cursor = "pointer";
        tr.innerHTML = html;
        tr.addEventListener("click", function () {
          openRegDetail(row.req_id);
        });
        tbody.appendChild(tr);
      }
    });

    // 删除已不在列表中的行
    for (var rid in existing) {
      if (Object.prototype.hasOwnProperty.call(existing, rid)) {
        var oldRow = existing[rid];
        if (oldRow.parentNode) oldRow.parentNode.removeChild(oldRow);
      }
    }
  }

  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function openRegDetail(reqId) {
    var data = state.registration;
    if (!data) return;
    var row = null;
    for (var i = 0; i < data.length; i++) {
      if (data[i].req_id === reqId) { row = data[i]; break; }
    }
    if (!row) return;

    state.regDetail = row;
    els.regDetailPanel.classList.remove("hidden");

    // 解析 host_info（JSON 或 "key:value, key:value"）
    var hostInfo = parseHostInfo(row.host_info);
    var hostInfoStr = "";
    for (var k in hostInfo) {
      if (Object.prototype.hasOwnProperty.call(hostInfo, k)) {
        hostInfoStr += k + ": " + hostInfo[k] + "\n";
      }
    }

    var tag = regTagLabel(row);
    var statusLabel = regStatusLabel(row.status);

    var body = els.regDetailBody;
    body.innerHTML = "";

    // 标题
    var title = el("h4", "id", row.project_id);
    body.appendChild(title);

    // 状态
    var statusEl = el("div", "reg-detail-section");
    statusEl.innerHTML = "<h4>状态</h4><div class=\"reg-detail-value\"><span class=\"status-badge " + row.status + "\">" + statusLabel + "</span></div>";
    body.appendChild(statusEl);

    // 拒绝原因（UI-003：拒绝后展示原因；数据源 /api/register/list 的 reject_reason）
    if (row.status === "rejected" && row.reject_reason) {
      var reasonEl = el("div", "reg-detail-section");
      reasonEl.innerHTML = "<h4>拒绝原因</h4><div class=\"reg-detail-value\">" + escapeHtml(row.reject_reason) + "</div>";
      body.appendChild(reasonEl);
    }

    // 项目 ID
    var projEl = el("div", "reg-detail-section");
    projEl.innerHTML = "<h4>项目 ID</h4><div class=\"reg-detail-value mono\">" + escapeHtml(row.project_id) + "</div>";
    body.appendChild(projEl);

    // 路径（后端 /api/register/list 暂不返回 path，缺失时显示 "—"）
    var pathEl = el("div", "reg-detail-section");
    pathEl.innerHTML = "<h4>路径</h4><div class=\"reg-detail-value mono\">" + escapeHtml(row.path || "—") + "</div>";
    body.appendChild(pathEl);

    // 主机信息
    var hostEl = el("div", "reg-detail-section");
    hostEl.innerHTML = "<h4>主机信息</h4><div class=\"reg-detail-value mono\">" + escapeHtml(hostInfoStr || row.host_info) + "</div>";
    body.appendChild(hostEl);

    // 申请时间
    var timeEl = el("div", "reg-detail-section");
    timeEl.innerHTML = "<h4>申请时间</h4><div class=\"reg-detail-value\">" + (row.created_at ? new Date(row.created_at * 1000).toLocaleString() : "—") + "</div>";
    body.appendChild(timeEl);

    // 注册码
    if (row.enrollment_code) {
      var codeEl = el("div", "reg-detail-section");
      codeEl.innerHTML = "<h4>注册码</h4><div class=\"reg-detail-value mono\">" + escapeHtml(row.enrollment_code) + "</div>";
      body.appendChild(codeEl);
    }

    // 标记
    var tagEl = el("div", "reg-detail-section");
    tagEl.innerHTML = "<h4>标记</h4><div class=\"reg-detail-value\"><span class=\"tag-badge " + tag.cls + "\">" + tag.label + "</span></div>";
    body.appendChild(tagEl);

    // 状态历史
    var histEl = el("div", "reg-detail-section");
    histEl.innerHTML = "<h4>状态历史</h4>";
    var histList = el("ul", "history-list");
    var histItems = [
      { status: "pending", label: "申请创建", ts: row.created_at }
    ];
    if (row.decided_at) {
      histItems.push({ status: row.status, label: statusLabel, ts: row.decided_at });
    }
    histItems.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = item.label + " · " + (item.ts ? new Date(item.ts * 1000).toLocaleString() : "—");
      histList.appendChild(li);
    });
    histEl.appendChild(histList);
    body.appendChild(histEl);

    // TASK-056: 渲染操作按钮
    renderRegActionButtons(row);
  }

  function closeRegDetail() {
    els.regDetailPanel.classList.add("hidden");
    state.regDetail = null;
    state.regActionPending = false;
    state.regPendingAction = null;
    if (els.regActionArea) els.regActionArea.innerHTML = "";
  }

  // —— TASK-056: 审批操作按钮渲染 ——
  function renderRegActionButtons(row) {
    var area = els.regActionArea;
    if (!area) return;
    area.innerHTML = "";

    var status = row.status;

    if (status === "pending") {
      // 显示「确认」「拒绝」
      var btnApprove = el("button", "reg-action-btn approve", "✅ 确认");
      btnApprove.addEventListener("click", function () { showRegConfirmModal("approve", row.req_id); });
      area.appendChild(btnApprove);

      var btnReject = el("button", "reg-action-btn reject", "❌ 拒绝");
      btnReject.addEventListener("click", function () { showRegRejectModal(row.req_id); });
      area.appendChild(btnReject);

    } else if (status === "approved") {
      // 显示「吊销」「轮换」
      var btnRevoke = el("button", "reg-action-btn revoke", "🔒 吊销");
      btnRevoke.addEventListener("click", function () { showRegConfirmModal("revoke", row.req_id); });
      area.appendChild(btnRevoke);

      var btnRenew = el("button", "reg-action-btn renew", "🔄 轮换");
      btnRenew.addEventListener("click", function () { showRegConfirmModal("renew", row.req_id); });
      area.appendChild(btnRenew);

    } else {
      // rejected/expired/revoked: 不显示操作按钮
      area.innerHTML = "";
    }

    // 如果操作进行中，禁用所有按钮
    if (state.regActionPending) {
      var btns = area.querySelectorAll(".reg-action-btn");
      for (var i = 0; i < btns.length; i++) {
        btns[i].disabled = true;
      }
    }
  }

  // —— TASK-056: 确认弹窗 ——
  function showRegConfirmModal(action, reqId) {
    var messages = {
      approve: { title: "确认审批", message: "将向该机器签发 token，确认？", showRemark: true },
      revoke: { title: "确认吊销", message: "吊销该机器的 token，确认？", showRemark: false },
      renew: { title: "确认轮换", message: "轮换该机器的 token，确认？", showRemark: false },
    };
    var info = messages[action];
    if (!info) return;

    els.regConfirmTitle.textContent = info.title;
    els.regConfirmMessage.textContent = info.message;
    els.regConfirmRemark.style.display = info.showRemark ? "" : "none";
    els.regConfirmRemark.value = "";
    els.regConfirmError.classList.add("hidden");
    els.regConfirmError.textContent = "";

    state.regPendingAction = { action: action, reqId: reqId };
    els.regConfirmModal.classList.remove("hidden");
    if (info.showRemark) els.regConfirmRemark.focus();
  }

  function closeRegConfirmModal() {
    els.regConfirmModal.classList.add("hidden");
    state.regPendingAction = null;
  }

  // —— TASK-056: 拒绝弹窗 ——
  function showRegRejectModal(reqId) {
    els.regRejectReason.value = "";
    els.regRejectError.classList.add("hidden");
    els.regRejectError.textContent = "";
    state.regPendingAction = { action: "reject", reqId: reqId };
    els.regRejectModal.classList.remove("hidden");
    els.regRejectReason.focus();
  }

  function closeRegRejectModal() {
    els.regRejectModal.classList.add("hidden");
    state.regPendingAction = null;
  }

  // —— TASK-056: 执行审批操作 ——
  function executeRegAction() {
    var pending = state.regPendingAction;
    if (!pending) return;

    // F3（REVIEW）：请求在途时忽略重复触发，防止双击弹窗确认按钮发出两个 POST
    if (state.regActionPending) return;

    var action = pending.action;
    var reqId = pending.reqId;

    // 获取 admin 密码
    var pwd = state.regAdminPwd;
    if (!pwd) {
      try { pwd = sessionStorage.getItem("aimonitor-reg-admin-pwd"); } catch (e) {}
      if (pwd) state.regAdminPwd = pwd;
    }
    if (!pwd) {
      closeRegConfirmModal();
      closeRegRejectModal();
      showAdminPwdModal();
      return;
    }

    // 验证拒绝原因
    if (action === "reject") {
      var reason = els.regRejectReason.value.trim();
      if (!reason) {
        els.regRejectError.textContent = "拒绝原因不能为空";
        els.regRejectError.classList.remove("hidden");
        els.regRejectReason.focus();
        return;
      }
    }

    // 设置 loading 状态
    state.regActionPending = true;
    var btns = document.querySelectorAll(".reg-action-btn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].disabled = true;
      btns[i].classList.add("loading");
    }

    // F3（REVIEW）：请求在途时禁用弹窗内全部按钮（含取消），
    // 避免双击确认重复提交、以及“取消后实际已执行”的误导
    var modalBtns = document.querySelectorAll(
      "#reg-confirm-modal .modal-actions button, #reg-reject-modal .modal-actions button"
    );
    for (var mi = 0; mi < modalBtns.length; mi++) {
      modalBtns[mi].disabled = true;
    }

    // 构建请求
    var url = "/api/register/" + encodeURIComponent(reqId) + "/" + action;
    var body = null;
    if (action === "reject") {
      body = JSON.stringify({ reason: els.regRejectReason.value.trim() });
    } else if (action === "approve") {
      var remark = els.regConfirmRemark.value.trim();
      body = remark ? JSON.stringify({ remark: remark }) : null;
    }

    fetch(url, {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + pwd,
        "Content-Type": "application/json"
      },
      body: body
    })
      .then(function (r) {
        if (r.status === 401) {
          handleRegAuthError();
          return null;
        }
        if (r.status === 409) {
          handleRegConflictError();
          return null;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (data === null) return; // 401/409 已处理

        // 操作成功
        state.regActionPending = false;
        closeRegConfirmModal();
        closeRegRejectModal();
        // F3（REVIEW）：成功路径也恢复弹窗/操作按钮状态（弹窗关闭但按钮 disabled 属性会残留）
        restoreRegActionButtons();

        // 更新本地状态
        var renewSuccessMsg = null;
        if (action === "approve") {
          if (state.regDetail) state.regDetail.status = "approved";
        } else if (action === "reject") {
          if (state.regDetail) {
            state.regDetail.status = "rejected";
            state.regDetail.reject_reason = els.regRejectReason.value.trim();
          }
        } else if (action === "revoke") {
          if (state.regDetail) state.regDetail.status = "revoked";
        } else if (action === "renew") {
          if (state.regDetail) state.regDetail.status = "approved";
          // UI-005：轮换成功消息在重渲染之后追加（见下）
          renewSuccessMsg = "新 token 已签发，agent 下次推送时自动领取";
        }

        // 重新渲染详情面板（renderRegActionButtons 会清空 action area）
        if (state.regDetail) {
          openRegDetail(state.regDetail.req_id);
        }

        // F1（REVIEW）：重渲染之后再追加轮换成功消息，避免被立即清空（UI-005）
        if (renewSuccessMsg && els.regActionArea) {
          els.regActionArea.appendChild(el("div", "reg-action-msg success", renewSuccessMsg));
        }

        // 刷新列表
        fetchRegistrationList();
      })
      .catch(function (e) {
        state.regActionPending = false;
        restoreRegActionButtons();

        // 在 action area 显示错误
        var errMsg = "网络错误，请重试";
        if (e.message.indexOf("HTTP") !== -1) errMsg = "服务器错误（" + e.message + "）";
        var errEl = el("div", "reg-action-msg error", errMsg);
        if (els.regActionArea) {
          // 清除之前的错误消息
          var oldMsgs = els.regActionArea.querySelectorAll(".reg-action-msg");
          for (var k = 0; k < oldMsgs.length; k++) oldMsgs[k].remove();
          els.regActionArea.appendChild(errEl);
        }

        // 更新弹窗中的错误
        if (!els.regConfirmModal.classList.contains("hidden")) {
          els.regConfirmError.textContent = errMsg;
          els.regConfirmError.classList.remove("hidden");
        }
        if (!els.regRejectModal.classList.contains("hidden")) {
          els.regRejectError.textContent = errMsg;
          els.regRejectError.classList.remove("hidden");
        }
      });
  }

  // F3（REVIEW）：恢复审批操作按钮 + 弹窗按钮的可用状态
  function restoreRegActionButtons() {
    var btns = document.querySelectorAll(".reg-action-btn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].disabled = false;
      btns[i].classList.remove("loading");
    }
    var modalBtns = document.querySelectorAll(
      "#reg-confirm-modal .modal-actions button, #reg-reject-modal .modal-actions button"
    );
    for (var mi = 0; mi < modalBtns.length; mi++) {
      modalBtns[mi].disabled = false;
    }
  }

  // —— TASK-056: 401 认证错误处理 ——
  function handleRegAuthError() {
    state.regActionPending = false;
    state.regAdminPwd = null;
    try { sessionStorage.removeItem("aimonitor-reg-admin-pwd"); } catch (e) {}

    closeRegConfirmModal();
    closeRegRejectModal();
    // TASK-057: 关闭注册码弹窗
    // F5（REVIEW）：函数存在性守卫——TASK-056 不把 TASK-057 代码的合并作为硬前提
    if (typeof closeGenerateCodeModal === "function") closeGenerateCodeModal();
    if (typeof closeRevokeCodeModal === "function") closeRevokeCodeModal();
    if (typeof closeCodeResultModal === "function") closeCodeResultModal();

    // 恢复按钮状态
    restoreRegActionButtons();

    // F4（REVIEW）：错误提示改由密码弹窗内的 #admin-pwd-error 展示
    // （写进 action area 会被弹窗遮罩挡住，用户看不到）
    showAdminPwdModal(true);
  }

  // —— TASK-056: 409 冲突错误处理 ——
  function handleRegConflictError() {
    state.regActionPending = false;
    closeRegConfirmModal();
    closeRegRejectModal();

    // 恢复按钮状态
    restoreRegActionButtons();

    // 在 action area 显示提示
    var errEl = el("div", "reg-action-msg error", "申请已处理，请刷新列表");
    if (els.regActionArea) {
      els.regActionArea.innerHTML = "";
      els.regActionArea.appendChild(errEl);
    }

    // 刷新列表
    fetchRegistrationList();
  }

  function showAdminPwdModal(showError) {
    els.adminPwdModal.classList.remove("hidden");
    // 401 重试时显示"密码错误"提示；首次打开时不显示（UI-003）
    els.adminPwdError.classList.toggle("hidden", !showError);
    els.adminPwdInput.value = "";
    els.adminPwdInput.focus();
  }

  function hideAdminPwdModal() {
    els.adminPwdModal.classList.add("hidden");
  }

  function confirmAdminPwd() {
    var pwd = els.adminPwdInput.value.trim();
    if (!pwd) return;
    state.regAdminPwd = pwd;
    try { sessionStorage.setItem("aimonitor-reg-admin-pwd", pwd); } catch (e) {}
    hideAdminPwdModal();
    fetchRegistrationList();
    // TASK-057: 如果在 codes tab，也刷新注册码列表
    if (state.regCodesTab === "codes") {
      fetchEnrollmentCodes();
    }
  }

  function fetchRegistrationList() {
    var pwd = state.regAdminPwd;
    if (!pwd) {
      showAdminPwdModal();
      return;
    }

    fetch("/api/register/list", {
      cache: "no-store",
      headers: { "Authorization": "Bearer " + pwd }
    })
      .then(function (r) {
        if (r.status === 401) {
          state.regAdminPwd = null;
          try { sessionStorage.removeItem("aimonitor-reg-admin-pwd"); } catch (e) {}
          showAdminPwdModal(true);
          return null;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (data === null) return; // 401 已处理
        state.registration = data;
        renderRegistrationPage();
      })
      .catch(function (e) {
        console.error("注册申请加载失败:", e.message);
      });
  }

  function startRegPolling() {
    stopRegPolling();
    state.regPollTimer = setTimeout(function poll() {
      fetchRegistrationList();
      state.regPollTimer = setTimeout(poll, 30000);
    }, 30000);
  }

  function stopRegPolling() {
    if (state.regPollTimer) {
      clearTimeout(state.regPollTimer);
      state.regPollTimer = null;
    }
  }

  // ===== TASK-057: 注册码管理 =====

  function maskCode(code) {
    // 掩码：保留部分字符，其余以 **** 遮盖（UI-002：ABC1****89）
    if (!code) return code;
    if (code.length <= 4) return "****";
    var prefix = code.slice(0, 4);
    var suffix = code.slice(-4);
    // F4（REVIEW）：≤8 字符的短 code 也做部分掩码（保留前 2 后 2），
    // 避免短 code 直接暴露原文
    if (code.length <= 8) {
      prefix = code.slice(0, 2);
      suffix = code.slice(-2);
    }
    return prefix + "****" + suffix;
  }

  function codeStatus(rec) {
    if (rec.revoked) return "revoked";
    if (rec.expire_at && rec.expire_at < Date.now() / 1000) return "expired";
    return "active";
  }

  function fetchEnrollmentCodes() {
    var pwd = state.regAdminPwd;
    if (!pwd) {
      try { pwd = sessionStorage.getItem("aimonitor-reg-admin-pwd"); } catch (e) {}
      if (pwd) state.regAdminPwd = pwd;
    }
    if (!pwd) {
      // F2（REVIEW）：与 fetchRegistrationList 对齐——缺密码时弹出认证框
      showAdminPwdModal();
      return;
    }

    fetch("/api/register/codes", {
      cache: "no-store",
      headers: { "Authorization": "Bearer " + pwd }
    })
      .then(function (r) {
        if (r.status === 401) {
          state.regAdminPwd = null;
          try { sessionStorage.removeItem("aimonitor-reg-admin-pwd"); } catch (e) {}
          // F2（REVIEW）：401 时提示重新认证（与 fetchRegistrationList 对齐）
          showAdminPwdModal(true);
          return null;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (data === null) return;
        state.regCodes = data;
        renderEnrollmentCodes();
      })
      .catch(function (e) {
        console.error("注册码加载失败:", e.message);
      });
  }

  function renderEnrollmentCodes() {
    var data = state.regCodes;
    if (!data) {
      if (els.regCodesTable) els.regCodesTable.innerHTML = "";
      if (els.regCodesEmpty) els.regCodesEmpty.classList.remove("hidden");
      if (els.regCodesCount) els.regCodesCount.textContent = "";
      return;
    }

    if (els.regCodesCount) {
      els.regCodesCount.textContent = data.length + " 个注册码";
    }

    if (!data.length) {
      if (els.regCodesTable) els.regCodesTable.innerHTML = "";
      if (els.regCodesEmpty) els.regCodesEmpty.classList.remove("hidden");
      return;
    }
    if (els.regCodesEmpty) els.regCodesEmpty.classList.add("hidden");

    var tbody = els.regCodesTable;
    if (!tbody) return;
    tbody.innerHTML = "";

    var now = Date.now() / 1000;

    data.forEach(function (rec) {
      var tr = document.createElement("tr");

      // 注册码（掩码，hover 显示完整）
      var tdCode = el("td");
      var masked = el("span", "reg-code-masked", maskCode(rec.code));
      masked.title = "完整注册码: " + rec.code;
      tdCode.appendChild(masked);
      tr.appendChild(tdCode);

      // 描述
      tr.appendChild(el("td", null, rec.description || "—"));

      // 允许项目
      tr.appendChild(el("td", null, rec.allowed_project_pattern || "—"));

      // 使用次数
      tr.appendChild(el("td", null, rec.use_count + "/" + rec.max_uses));

      // 过期时间
      var expireStr = "—";
      if (rec.expire_at) {
        var d = new Date(rec.expire_at * 1000);
        expireStr = d.toLocaleDateString();
      }
      tr.appendChild(el("td", null, expireStr));

      // 状态
      var tdStatus = el("td");
      var st = codeStatus(rec);
      var stLabels = { active: "有效", revoked: "已吊销", expired: "已过期" };
      tdStatus.appendChild(el("span", "reg-code-status " + st, stLabels[st] || st));
      tr.appendChild(tdStatus);

      // 操作
      var tdAction = el("td");
      if (st === "active") {
        var revokeBtn = el("button", "reg-code-revoke-btn", "吊销");
        revokeBtn.addEventListener("click", function (e) {
          e.stopPropagation();
          showRevokeCodeModal(rec.code);
        });
        tdAction.appendChild(revokeBtn);
      } else {
        tdAction.appendChild(el("span", "muted", "—"));
      }
      tr.appendChild(tdAction);

      tbody.appendChild(tr);
    });
  }

  // —— 子 tab 切换 ——
  function switchRegTab(tab) {
    state.regCodesTab = tab;

    // 更新 tab 按钮状态
    var tabs = els.regTabs;
    if (tabs) {
      for (var i = 0; i < tabs.length; i++) {
        tabs[i].classList.toggle("active", tabs[i].getAttribute("data-reg-tab") === tab);
      }
    }

    // 切换 pane
    if (els.regQueuePane) els.regQueuePane.classList.toggle("hidden", tab !== "queue");
    if (els.regCodesPane) els.regCodesPane.classList.toggle("hidden", tab !== "codes");

    // 切换到 codes 时加载数据
    if (tab === "codes") {
      fetchEnrollmentCodes();
    }
  }

  // —— 生成注册码弹窗 ——
  function showGenerateCodeModal() {
    // 重置表单
    if (els.regCodeGenDesc) els.regCodeGenDesc.value = "";
    if (els.regCodeGenProject) els.regCodeGenProject.value = "";
    if (els.regCodeGenMaxUses) els.regCodeGenMaxUses.value = "1";
    if (els.regCodeGenExpire) els.regCodeGenExpire.value = "";
    if (els.regCodeGenDescErr) els.regCodeGenDescErr.classList.add("hidden");
    if (els.regCodeGenMaxUsesErr) els.regCodeGenMaxUsesErr.classList.add("hidden");
    if (els.regCodeGenError) {
      els.regCodeGenError.classList.add("hidden");
      els.regCodeGenError.textContent = "";
    }

    if (els.regCodeGenerateModal) els.regCodeGenerateModal.classList.remove("hidden");
    if (els.regCodeGenDesc) els.regCodeGenDesc.focus();
  }

  function closeGenerateCodeModal() {
    if (els.regCodeGenerateModal) els.regCodeGenerateModal.classList.add("hidden");
  }

  function executeGenerateCode() {
    // F3（REVIEW）：请求在途守卫——Enter 键与按钮点击互不感知，
    // 快速连按会发出多个 generate 请求（非幂等端点）
    if (state.regCodeGenPending) return;

    // 校验
    var desc = els.regCodeGenDesc ? els.regCodeGenDesc.value.trim() : "";
    if (!desc) {
      if (els.regCodeGenDescErr) els.regCodeGenDescErr.classList.remove("hidden");
      if (els.regCodeGenDesc) els.regCodeGenDesc.focus();
      return;
    }
    if (els.regCodeGenDescErr) els.regCodeGenDescErr.classList.add("hidden");

    var maxUses = parseInt(els.regCodeGenMaxUses ? els.regCodeGenMaxUses.value : "1", 10);
    if (isNaN(maxUses) || maxUses < 1) {
      if (els.regCodeGenMaxUsesErr) els.regCodeGenMaxUsesErr.classList.remove("hidden");
      if (els.regCodeGenMaxUses) els.regCodeGenMaxUses.focus();
      return;
    }
    if (els.regCodeGenMaxUsesErr) els.regCodeGenMaxUsesErr.classList.add("hidden");

    var project = els.regCodeGenProject ? els.regCodeGenProject.value.trim() : "";
    var expireDate = els.regCodeGenExpire ? els.regCodeGenExpire.value : "";
    var expireAt = null;
    if (expireDate) {
      // 将日期字符串转为当天结束时的 timestamp
      var d = new Date(expireDate + "T23:59:59");
      expireAt = d.getTime() / 1000;
    }

    // 获取 admin 密码
    var pwd = state.regAdminPwd;
    if (!pwd) {
      try { pwd = sessionStorage.getItem("aimonitor-reg-admin-pwd"); } catch (e) {}
    }
    if (!pwd) {
      closeGenerateCodeModal();
      showAdminPwdModal();
      return;
    }

    // 禁用按钮（F3：同步置在途标记）
    state.regCodeGenPending = true;
    if (els.regCodeGenConfirm) {
      els.regCodeGenConfirm.disabled = true;
      els.regCodeGenConfirm.textContent = "生成中…";
    }

    var body = {
      description: desc,
      max_uses: maxUses,
    };
    if (project) body.allowed_project = project;
    if (expireAt) body.expire_at = expireAt;

    fetch("/api/register/codes/generate", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + pwd,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        if (r.status === 401) {
          handleRegAuthError();
          return null;
        }
        if (!r.ok) return r.json().then(function (err) { throw new Error(err.message || "HTTP " + r.status); });
        return r.json();
      })
      .then(function (data) {
        if (data === null) return;

        // 关闭生成弹窗
        closeGenerateCodeModal();

        // 显示结果弹窗
        showCodeResult(data.code);

        // 刷新列表
        fetchEnrollmentCodes();
      })
      .catch(function (e) {
        if (els.regCodeGenError) {
          els.regCodeGenError.textContent = "生成失败: " + e.message;
          els.regCodeGenError.classList.remove("hidden");
        }
      })
      .finally(function () {
        state.regCodeGenPending = false;
        if (els.regCodeGenConfirm) {
          els.regCodeGenConfirm.disabled = false;
          els.regCodeGenConfirm.textContent = "生成";
        }
      });
  }

  // —— 生成结果展示 ——
  function showCodeResult(code) {
    if (els.regCodeResultValue) els.regCodeResultValue.textContent = code;
    if (els.regCodeCopiedMsg) els.regCodeCopiedMsg.classList.add("hidden");
    if (els.regCodeResultModal) els.regCodeResultModal.classList.remove("hidden");
  }

  function closeCodeResultModal() {
    if (els.regCodeResultModal) els.regCodeResultModal.classList.add("hidden");
  }

  function copyCodeResult() {
    var code = els.regCodeResultValue ? els.regCodeResultValue.textContent : "";
    if (!code) return;

    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(code).then(function () {
        if (els.regCodeCopiedMsg) els.regCodeCopiedMsg.classList.remove("hidden");
      }).catch(function () {
        // fallback
        fallbackCopy(code);
      });
    } else {
      fallbackCopy(code);
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      if (els.regCodeCopiedMsg) els.regCodeCopiedMsg.classList.remove("hidden");
    } catch (e) {}
    document.body.removeChild(ta);
  }

  // —— 吊销确认弹窗 ——
  function showRevokeCodeModal(code) {
    state.regCodePendingRevoke = code;
    if (els.regCodeRevokeMessage) {
      els.regCodeRevokeMessage.textContent = "确认吊销注册码 " + maskCode(code) + "？此操作不可撤销。";
    }
    if (els.regCodeRevokeError) {
      els.regCodeRevokeError.classList.add("hidden");
      els.regCodeRevokeError.textContent = "";
    }
    if (els.regCodeRevokeModal) els.regCodeRevokeModal.classList.remove("hidden");
  }

  function closeRevokeCodeModal() {
    if (els.regCodeRevokeModal) els.regCodeRevokeModal.classList.add("hidden");
    state.regCodePendingRevoke = null;
  }

  function executeRevokeCode() {
    var code = state.regCodePendingRevoke;
    if (!code) return;

    var pwd = state.regAdminPwd;
    if (!pwd) {
      try { pwd = sessionStorage.getItem("aimonitor-reg-admin-pwd"); } catch (e) {}
    }
    if (!pwd) {
      closeRevokeCodeModal();
      showAdminPwdModal();
      return;
    }

    if (els.regCodeRevokeConfirm) {
      els.regCodeRevokeConfirm.disabled = true;
      els.regCodeRevokeConfirm.textContent = "吊销中…";
    }

    fetch("/api/register/codes/" + encodeURIComponent(code) + "/revoke", {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + pwd,
        "Content-Type": "application/json"
      }
    })
      .then(function (r) {
        if (r.status === 401) {
          handleRegAuthError();
          return null;
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (data === null) return;
        closeRevokeCodeModal();
        fetchEnrollmentCodes();
      })
      .catch(function (e) {
        if (els.regCodeRevokeError) {
          els.regCodeRevokeError.textContent = "吊销失败: " + e.message;
          els.regCodeRevokeError.classList.remove("hidden");
        }
      })
      .finally(function () {
        if (els.regCodeRevokeConfirm) {
          els.regCodeRevokeConfirm.disabled = false;
          els.regCodeRevokeConfirm.textContent = "确认吊销";
        }
      });
  }

  // —— 任务详情抽屉 ——
  function field(label, text, badgeCls) {
    var d = el("div");
    d.appendChild(el("span", "k", label));
    var v = el("span", "v");
    if (badgeCls) v.appendChild(badge(text, badgeCls, text));
    else v.textContent = text || "—";
    d.appendChild(v);
    return d;
  }

  // —— 完整正文渲染（TASK-024：detail 字段 → 依赖/验收 checklist/章节/关联记录）——
  function renderTaskDetail(detail) {
    if (!detail) return;

    // 依赖
    var deps = detail.dependencies || [];
    if (deps.length) {
      var dSec = el("div", "sec");
      dSec.appendChild(el("h5", null, "依赖"));
      var dUl = el("ul");
      deps.forEach(function (d) { dUl.appendChild(el("li", null, d)); });
      dSec.appendChild(dUl);
      els.detailBody.appendChild(dSec);
    }

    // 验收标准 checklist（含勾选态）
    var acc = detail.acceptance || [];
    if (acc.length) {
      var aSec = el("div", "sec");
      aSec.appendChild(el("h5", null, "验收标准"));
      var aUl = el("ul", "acceptance");
      acc.forEach(function (a) {
        var li = el("li", a.checked ? "checked" : "");
        li.appendChild(el("span", "acc-box", a.checked ? "☑" : "☐"));
        li.appendChild(document.createTextNode(a.text || ""));
        aUl.appendChild(li);
      });
      aSec.appendChild(aUl);
      els.detailBody.appendChild(aSec);
    }

    // 正文章节（验收标准已用 checklist 渲染，跳过）
    (detail.sections || []).forEach(function (s) {
      if (s.heading === "验收标准") return;
      var sec = el("div", "sec");
      sec.appendChild(el("h5", null, s.heading));
      var p = el("div", "sec-body");
      p.textContent = s.body || "—";
      sec.appendChild(p);
      els.detailBody.appendChild(sec);
    });

    // 关联验证/审查记录（按 result 着色：pass 绿，其余警示）
    var ver = detail.verification || [];
    var rev = detail.reviews || [];
    if (ver.length || rev.length) {
      var rSec = el("div", "sec");
      rSec.appendChild(el("h5", null, "验证 / 审查"));
      var rUl = el("ul");
      function recLi(r) {
        var cls = r.result === "pass" ? "rec-ok" : "rec-warn";
        var label = r.name + " · " + (r.result || "?");
        if (r.date) label += " · " + r.date;
        rUl.appendChild(el("li", "rec " + cls, label));
      }
      ver.forEach(recLi);
      rev.forEach(recLi);
      rSec.appendChild(rUl);
      els.detailBody.appendChild(rSec);
    }
  }

  function openTaskDetail(t) {
    els.detailBody.innerHTML = "";
    els.detailBody.appendChild(el("div", "id", t.slug || t.id));
    els.detailBody.appendChild(el("h4", null, t.name || t.id));
    if (t.description && t.description !== t.name) {
      els.detailBody.appendChild(el("div", "desc", t.description));
    }
    var kv = el("div", "kv");
    kv.appendChild(field("状态", STATUS_LABEL[t.status] || t.status || "—", "st-" + t.status));
    kv.appendChild(field("优先级", t.priority || "—", "st-" + t.priority));
    kv.appendChild(field("风险", t.risk || "—", "st-" + t.risk));
    kv.appendChild(field("Assignee", t.assignee || "—"));
    kv.appendChild(field("Reviewer", t.reviewer || "—"));
    kv.appendChild(field("更新", t.updated || "—"));
    els.detailBody.appendChild(kv);

    renderTaskDetail(t.detail);

    state.activeTaskId = t.id;
    markActiveRow();
    els.detailPanel.classList.remove("hidden");
  }

  function markActiveRow() {
    var rows = document.querySelectorAll("#task-table tbody tr.task-row");
    for (var i = 0; i < rows.length; i++) {
      var on = rows[i].getAttribute("data-task-id") === state.activeTaskId;
      rows[i].classList.toggle("active", on);
    }
  }

  function closeTaskDetail() {
    state.activeTaskId = null;
    markActiveRow();
    els.detailPanel.classList.add("hidden");
  }

  // —— 导航 ——
  var NAV_SECTION = {
    overview: null,       // 滚动到顶部
    tasks: "tasks-panel",
    exec: "exec-panel",
    sessions: "sessions-panel",  // TASK-073: Session 日志查看页
    trend: "trend-panel",
    alerts: "alerts",
    registration: "registration-page"
  };

  function scrollToSection(id) {
    if (!id) { window.scrollTo({ top: 0, behavior: "smooth" }); return; }
    if (id === "alerts") {
      var banner = els.alertBanner;
      if (!banner.classList.contains("hidden")) banner.scrollIntoView({ behavior: "smooth", block: "center" });
      else document.getElementById("projects-panel").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    var target = document.getElementById(id);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function showMainContent(show) {
    var mainSections = [
      document.getElementById("overview"),
      document.getElementById("trend-panel"),
      document.querySelector(".cols"),
      document.getElementById("projects-panel"),
      document.getElementById("tasks-panel")
    ];
    mainSections.forEach(function (s) {
      if (s) s.style.display = show ? "" : "none";
    });
    // 注册页面
    if (els.regPage) els.regPage.classList.toggle("hidden", !show);
  }

  function bindNav() {
    var items = document.querySelectorAll(".nav-item[data-nav]");
    for (var i = 0; i < items.length; i++) {
      items[i].addEventListener("click", function () {
        var key = this.getAttribute("data-nav");
        state.activeNav = key;
        for (var j = 0; j < items.length; j++) items[j].classList.remove("active");
        this.classList.add("active");

        if (key === "registration") {
          // 注册申请页面：隐藏主内容，显示注册页面
          var mainSections = [
            document.getElementById("overview"),
            document.getElementById("trend-panel"),
            document.querySelector(".cols"),
            document.getElementById("projects-panel"),
            document.getElementById("tasks-panel")
          ];
          mainSections.forEach(function (s) {
            if (s) s.style.display = "none";
          });
          if (els.regPage) {
            els.regPage.classList.remove("hidden");
            window.scrollTo({ top: 0, behavior: "smooth" });
          }
          // 关闭任务详情抽屉
          if (!els.detailPanel.classList.contains("hidden")) closeTaskDetail();
          if (!els.regDetailPanel.classList.contains("hidden")) closeRegDetail();
          // TASK-057: 重置子 tab 到申请队列
          switchRegTab("queue");
          // 加载注册列表
          var saved = null;
          try { saved = sessionStorage.getItem("aimonitor-reg-admin-pwd"); } catch (e) {}
          if (saved) state.regAdminPwd = saved;
          fetchRegistrationList();
          startRegPolling();
        } else {
          // 其他页面：恢复主内容显示，隐藏注册页面
          showMainContent(true);
          stopRegPolling();
          scrollToSection(NAV_SECTION[key] || null);
          // TASK-073: Session 页进入即加载 + 轮询；离开即停（不产生后台流量）
          if (key === "sessions") {
            startSessionPolling();
          } else {
            stopSessionPolling();
          }
        }
      });
    }
  }

  // —— 渲染主入口 ——
  function render() {
    if (!state.data) return;
    var p = projectById(state.currentProjectId);
    if (!p) return;
    clearError();
    var alerts = deriveAlerts();
    renderTopbar();
    renderProjectSelect();
    renderSidebar(alerts);
    renderAlertBanner(alerts);
    renderProjectTable();
    renderOverview(p);
    renderTasks(p);
    renderEvents(p);
    renderFocus(p);
    renderStatusBars(p);
  }

  // —— 数据刷新 ——
  function refresh() {
    fetch("/api/status", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        state.data = data;
        if (!state.currentProjectId && data.projects.length) {
          state.currentProjectId = data.projects[0].id;
        }
        state.pollMs = (data.poll_interval_seconds || 30) * 1000;
        render();
        refreshTasks(); // 全量刷新后按当前筛选重取任务表格
        loadTrend(true); // TASK-027: 趋势图随轮询刷新（新快照入库后更新）
        schedule();
      })
      .catch(function (e) {
        showError("获取数据失败: " + e.message);
        schedule();
      });
  }

  var timer = null;
  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refresh, state.pollMs);
  }

  // —— Session 日志（TASK-073：agent 推送 session 增量 → /api/projects/:id/sessions；
  //    消息级粒度 + agent 默认 30s 推送周期，前端 5s 轮询满足 ≤10s 级感知；
  //    SSE 增益有限故选轮询——选型决策见任务卡备注与 aibase TASK-104 评估结论） ——

  function sessionUrl() {
    return "/api/projects/" + encodeURIComponent(state.currentProjectId) + "/sessions";
  }

  function fillSessionSelect(sel, placeholder, items, current) {
    sel.innerHTML = "";
    var ph = document.createElement("option");
    ph.value = ""; ph.textContent = placeholder;
    sel.appendChild(ph);
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = it.v; o.textContent = it.label;
      if (it.v === current) o.selected = true;
      sel.appendChild(o);
    });
  }

  function sessionTotalLines(tasks) {
    return tasks.reduce(function (n, t) {
      return n + t.files.reduce(function (m, f) { return m + f.line_count; }, 0);
    }, 0);
  }

  function loadSessions() {
    var pid = state.currentProjectId;
    if (!pid) return;
    fetch(sessionUrl(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (state.currentProjectId !== pid) return; // 项目已切换，丢弃防串台
        var projectChanged = state.session.projectId !== pid;
        state.session.projectId = pid;
        state.session.summary = data;
        if (projectChanged) { state.session.task = null; state.session.file = null; }
        var tasks = data.tasks || [];
        fillSessionSelect(els.sessionTask, "选择任务…",
          tasks.map(function (t) {
            return { v: t.task_id,
                     label: t.task_id + "（" + sessionTotalLines([t]) + " 行）" };
          }), state.session.task);
        var cur = null;
        for (var i = 0; i < tasks.length; i++) {
          if (tasks[i].task_id === state.session.task) cur = tasks[i];
        }
        var files = cur ? cur.files : [];
        if (!state.session.file && files.length === 1) state.session.file = files[0].name;
        fillSessionSelect(els.sessionFile, "选择文件…",
          files.map(function (f) {
            return { v: f.name, label: f.name + "（" + f.line_count + " 行）" };
          }), state.session.file);
        els.navSessionCount.textContent = tasks.length ? String(sessionTotalLines(tasks)) : "";
        if (state.session.task && state.session.file) {
          loadSessionLines();
        } else {
          state.session.lines = null;
          renderSessionPlaceholder("选择任务与文件查看会话内容");
        }
      })
      .catch(function (e) {
        renderSessionPlaceholder("获取 sessions 失败: " + e.message);
      });
  }

  function loadSessionLines() {
    var pid = state.currentProjectId;
    var q = sessionUrl() + "?task=" + encodeURIComponent(state.session.task) +
            "&file=" + encodeURIComponent(state.session.file) + "&limit=200";
    fetch(q, { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then(function (data) {
        if (state.currentProjectId !== pid) return;
        state.session.lines = data;
        renderSessionFeed(data);
      })
      .catch(function (e) {
        renderSessionPlaceholder("获取 session 行失败: " + e.message);
      });
  }

  function renderSessionPlaceholder(msg) {
    els.sessionFeed.innerHTML = "";
    var d = document.createElement("div");
    d.className = "trend-placeholder"; d.textContent = msg;
    els.sessionFeed.appendChild(d);
    els.sessionMeta.textContent = "";
  }

  function sessionPartBody(p) {
    if (p == null) return "";
    if (typeof p === "string") return p;
    if (p.type === "toolCall") {
      var args = p.arguments || p.input || p.args || p.payload;
      var body = args ? JSON.stringify(args) : "";
      if (body.length > 500) body = body.slice(0, 500) + "…[truncated]";
      return (p.name || p.toolName || "tool") + " " + body;
    }
    if (p.type === "toolResult") {
      var out = p.output != null ? String(p.output) : JSON.stringify(p);
      if (out.length > 500) out = out.slice(0, 500) + "…[truncated]";
      return out;
    }
    return p.text || p.thinking || p.content || "";
  }

  function sessionPartClass(p) {
    if (p && typeof p === "object") {
      if (p.type === "thinking") return "session-part part-thinking";
      if (p.type === "toolCall" || p.type === "toolResult") return "session-part part-tool";
    }
    return "session-part part-text";
  }

  function appendSessionLine(feed, line) {
    var row = document.createElement("div");
    if (!line.ok) {
      row.className = "session-msg session-raw";
      var pre = document.createElement("pre");
      pre.textContent = "#" + line.line_no + "（非 JSON） " + line.text;
      row.appendChild(pre);
      feed.appendChild(row);
      return;
    }
    var d = line.data || {};
    if (d.type === "message" && d.message) {
      var m = d.message;
      var role = m.role || "unknown";
      row.className = "session-msg role-" + role;
      var head = document.createElement("div");
      head.className = "session-head";
      var badge = document.createElement("span");
      badge.className = "session-role"; badge.textContent = role;
      var ts = document.createElement("span");
      ts.className = "session-ts";
      ts.textContent = "#" + line.line_no + (d.timestamp ? " · " + d.timestamp : "");
      head.appendChild(badge); head.appendChild(ts);
      row.appendChild(head);
      var parts = Array.isArray(m.content) ? m.content
                : (m.content != null ? [m.content] : []);
      parts.forEach(function (p) {
        var pd = document.createElement("div");
        pd.className = sessionPartClass(p);
        pd.textContent = sessionPartBody(p);
        row.appendChild(pd);
      });
      if (!parts.length) {
        var empty = document.createElement("div");
        empty.className = "session-part part-text";
        empty.textContent = "（空内容）";
        row.appendChild(empty);
      }
    } else {
      // 系统元数据行（session/model_change/thinking_level_change/…）：单行弱化展示
      row.className = "session-msg session-sys";
      var sys = document.createElement("div");
      sys.className = "session-sys-line";
      sys.textContent = "#" + line.line_no + " · " + (d.type || "raw") +
                        (d.modelId ? " · " + d.modelId : "") +
                        (d.thinkingLevel ? " · thinking=" + d.thinkingLevel : "");
      row.appendChild(sys);
    }
    feed.appendChild(row);
  }

  function renderSessionFeed(data) {
    els.sessionFeed.innerHTML = "";
    var lines = (data && data.lines) || [];
    if (!lines.length) {
      renderSessionPlaceholder("暂无行（等待 agent 推送增量）");
      return;
    }
    var frag = document.createDocumentFragment();
    // 用临时容器复用 appendSessionLine 的 DOM 构建逻辑
    var tmp = document.createElement("div");
    lines.forEach(function (line) { appendSessionLine(tmp, line); });
    while (tmp.firstChild) frag.appendChild(tmp.firstChild);
    els.sessionFeed.appendChild(frag);
    els.sessionFeed.scrollTop = els.sessionFeed.scrollHeight; // 实时视角：跟随最新行
    var meta = "最近 " + lines.length + " 行（共 " + data.line_count + "） · " +
               (data.updated_at ? new Date(data.updated_at * 1000).toLocaleTimeString() : "—");
    if (data.truncated) meta = "⚠ 积压未送达（truncated） · " + meta;
    els.sessionMeta.textContent = meta;
  }

  function startSessionPolling() {
    stopSessionPolling();
    loadSessions();
    state.session.timer = setInterval(loadSessions, 5000);
  }

  function stopSessionPolling() {
    if (state.session.timer) {
      clearInterval(state.session.timer);
      state.session.timer = null;
    }
  }

  // —— 事件绑定 ——
  els.refreshBtn.addEventListener("click", refresh);
  // TASK-073: Session 选择器 + 实时开关
  els.sessionTask.addEventListener("change", function () {
    state.session.task = this.value || null;
    state.session.file = null;
    loadSessions();
  });
  els.sessionFile.addEventListener("change", function () {
    state.session.file = this.value || null;
    if (state.session.file) loadSessionLines();
    else renderSessionPlaceholder("选择文件查看会话内容");
  });
  els.sessionLiveToggle.addEventListener("change", function () {
    if (this.checked && state.activeNav === "sessions") startSessionPolling();
    else stopSessionPolling();
  });
  els.detailClose.addEventListener("click", closeTaskDetail);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      // TASK-057: 注册码弹窗优先
      if (!els.regCodeResultModal.classList.contains("hidden")) { closeCodeResultModal(); return; }
      if (!els.regCodeRevokeModal.classList.contains("hidden")) { closeRevokeCodeModal(); return; }
      if (!els.regCodeGenerateModal.classList.contains("hidden")) { closeGenerateCodeModal(); return; }
      if (!els.regConfirmModal.classList.contains("hidden")) { closeRegConfirmModal(); return; }
      if (!els.regRejectModal.classList.contains("hidden")) { closeRegRejectModal(); return; }
      if (!els.detailPanel.classList.contains("hidden")) { closeTaskDetail(); return; }
      if (!els.regDetailPanel.classList.contains("hidden")) { closeRegDetail(); return; }
      if (els.sidebar && els.sidebar.classList.contains("open")) { closeSidebar(); return; }
      if (!els.adminPwdModal.classList.contains("hidden")) {
        hideAdminPwdModal();
        if (!state.regAdminPwd) {
          state.activeNav = "overview";
          showMainContent(true);
          stopRegPolling();
        }
        return;
      }
    }
  });
  els.select.addEventListener("change", function () { setCurrentProject(els.select.value); });
  els.themeToggle.addEventListener("click", function () {
    setTheme(state.theme === "dark" ? "light" : "dark");
  });

  // —— 注册申请事件绑定（TASK-055）——
  if (els.regRefreshBtn) {
    els.regRefreshBtn.addEventListener("click", function () {
      fetchRegistrationList();
    });
  }
  if (els.regDetailClose) {
    els.regDetailClose.addEventListener("click", closeRegDetail);
  }
  if (els.adminPwdConfirm) {
    els.adminPwdConfirm.addEventListener("click", confirmAdminPwd);
  }
  if (els.adminPwdCancel) {
    els.adminPwdCancel.addEventListener("click", function () {
      hideAdminPwdModal();
      // 如果取消且没有密码，切回总览
      if (!state.regAdminPwd) {
        state.activeNav = "overview";
        showMainContent(true);
        stopRegPolling();
        var navItems = document.querySelectorAll(".nav-item[data-nav]");
        for (var ni = 0; ni < navItems.length; ni++) {
          navItems[ni].classList.remove("active");
          if (navItems[ni].getAttribute("data-nav") === "overview") navItems[ni].classList.add("active");
        }
      }
    });
  }
  if (els.adminPwdInput) {
    els.adminPwdInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") confirmAdminPwd();
    });
  }

  // —— TASK-056: 审批操作事件绑定 ——
  if (els.regConfirmOk) {
    els.regConfirmOk.addEventListener("click", executeRegAction);
  }
  if (els.regConfirmCancel) {
    els.regConfirmCancel.addEventListener("click", closeRegConfirmModal);
  }
  if (els.regRejectOk) {
    els.regRejectOk.addEventListener("click", executeRegAction);
  }
  if (els.regRejectCancel) {
    els.regRejectCancel.addEventListener("click", closeRegRejectModal);
  }
  // 回车确认弹窗
  if (els.regConfirmRemark) {
    els.regConfirmRemark.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); executeRegAction(); }
    });
  }
  if (els.regRejectReason) {
    els.regRejectReason.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); executeRegAction(); }
    });
  }

  // —— TASK-057: 注册码管理事件绑定 ——
  // 子 tab 切换（事件委托到注册页面容器）
  if (els.regPage) {
    els.regPage.addEventListener("click", function (e) {
      var tab = e.target.closest(".reg-tab");
      if (tab) {
        var tabName = tab.getAttribute("data-reg-tab");
        if (tabName) switchRegTab(tabName);
      }
    });
  }
  if (els.regCodeGenerateBtn) {
    els.regCodeGenerateBtn.addEventListener("click", showGenerateCodeModal);
  }
  if (els.regCodeGenCancel) {
    els.regCodeGenCancel.addEventListener("click", closeGenerateCodeModal);
  }
  if (els.regCodeGenConfirm) {
    els.regCodeGenConfirm.addEventListener("click", executeGenerateCode);
  }
  if (els.regCodeResultClose) {
    els.regCodeResultClose.addEventListener("click", closeCodeResultModal);
  }
  if (els.regCodeResultCopy) {
    els.regCodeResultCopy.addEventListener("click", copyCodeResult);
  }
  if (els.regCodeRevokeCancel) {
    els.regCodeRevokeCancel.addEventListener("click", closeRevokeCodeModal);
  }
  if (els.regCodeRevokeConfirm) {
    els.regCodeRevokeConfirm.addEventListener("click", executeRevokeCode);
  }

  // 生成弹窗回车提交
  if (els.regCodeGenDesc) {
    els.regCodeGenDesc.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); executeGenerateCode(); }
    });
  }

  // —— 移动端侧栏抽屉（TASK-030）——
  function openSidebar() {
    if (!els.sidebar || !els.sidebarMask) return; // MIN-001：元素缺失时安全返回
    els.sidebar.classList.add("open");
    els.sidebarMask.classList.remove("hidden");
  }
  function closeSidebar() {
    if (!els.sidebar || !els.sidebarMask) return; // MIN-001
    els.sidebar.classList.remove("open");
    els.sidebarMask.classList.add("hidden");
  }
  if (els.menuToggle) {
    els.menuToggle.addEventListener("click", function () {
      if (els.sidebar && els.sidebar.classList.contains("open")) closeSidebar(); else openSidebar();
    });
  }
  if (els.sidebarMask) {
    els.sidebarMask.addEventListener("click", closeSidebar);
  }
  // 导航项点击后在移动端自动关闭抽屉（避免遮挡内容）
  // 事件委托到侧栏容器：.nav-project 由 renderSidebar() 动态创建，初始化时
  // querySelectorAll 绑不到新按钮；委托同时覆盖静态 .nav-item[data-nav] 与动态项目按钮（REVIEW MED-001）。
  if (els.sidebar) {
    els.sidebar.addEventListener("click", function (e) {
      var t = e.target;
      while (t && t !== els.sidebar) {
        if (t.classList && (t.classList.contains("nav-project") || t.hasAttribute("data-nav"))) {
          closeSidebar();
          return;
        }
        t = t.parentNode;
      }
    });
  }
  var searchTimer = null; // TASK-025: 搜索防抖
  els.taskSearch.addEventListener("input", function () {
    state.filters.q = els.taskSearch.value.trim();
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(refreshTasks, 200); // 防抖：避免每击键一次请求
  });
  els.filterStatus.addEventListener("change", function () { state.filters.status = els.filterStatus.value; refreshTasks(); });
  els.filterPriority.addEventListener("change", function () { state.filters.priority = els.filterPriority.value; refreshTasks(); });
  els.filterAssignee.addEventListener("change", function () { state.filters.assignee = els.filterAssignee.value; refreshTasks(); });

  // TASK-055: 初始隐藏注册页面和密码弹窗
  if (els.regPage) els.regPage.classList.add("hidden");
  if (els.adminPwdModal) els.adminPwdModal.classList.add("hidden");

  bindNav();
  bindRangeSeg();
  initTheme(); // TASK-028: localStorage 记忆优先 → prefers-color-scheme → 默认 dark
  refresh(); // 首次加载
})();

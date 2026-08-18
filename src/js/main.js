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
    pollMs: 30000
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
    trendCompletionMeta: document.getElementById("trend-completion-meta"),
    trendEventMeta: document.getElementById("trend-event-meta"),
    focus: document.getElementById("focus"),
    statusBars: document.getElementById("status-bars"),
    statusLegend: document.getElementById("status-legend"),
    detailPanel: document.getElementById("detail-panel"),
    detailBody: document.getElementById("detail-body"),
    detailClose: document.getElementById("detail-close")
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
    items.sort(function (a, b) { return (b.ev.ts || 0) - (a.ev.ts || 0); });

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
      var taskText = (it.ev.task && it.ev.task !== "-" ? it.ev.task + " → " : "") + (it.ev.outcome || "?");
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
    trend: "trend-panel",
    alerts: "alerts"
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

  function bindNav() {
    var items = document.querySelectorAll(".nav-item[data-nav]");
    for (var i = 0; i < items.length; i++) {
      items[i].addEventListener("click", function () {
        var key = this.getAttribute("data-nav");
        state.activeNav = key;
        for (var j = 0; j < items.length; j++) items[j].classList.remove("active");
        this.classList.add("active");
        scrollToSection(NAV_SECTION[key] || null);
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

  // —— 事件绑定 ——
  els.refreshBtn.addEventListener("click", refresh);
  els.detailClose.addEventListener("click", closeTaskDetail);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !els.detailPanel.classList.contains("hidden")) closeTaskDetail();
  });
  els.select.addEventListener("change", function () { setCurrentProject(els.select.value); });
  els.themeToggle.addEventListener("click", function () {
    setTheme(state.theme === "dark" ? "light" : "dark");
  });

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

  bindNav();
  bindRangeSeg();
  initTheme(); // TASK-028: localStorage 记忆优先 → prefers-color-scheme → 默认 dark
  refresh(); // 首次加载
})();

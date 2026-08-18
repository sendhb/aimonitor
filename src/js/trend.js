// aimonitor — 趋势图纯函数（TASK-027；零第三方依赖）
// 浏览器全局 AiTrend + Node module.exports 双形态（UMD），便于 node 单测。
// 数据源契约：GET /api/history 的 points（见 docs/MONITOR-SPEC.md §4.2）。
// 本文件只含纯函数（无 DOM），SVG 元素构建在 src/js/main.js（createElementNS）。
(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.AiTrend = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var STATUS_KEYS = ["open", "in-progress", "in-review", "blocked", "done", "cancelled"];

  // —— 序列推导 ——

  // 完成率序列：每点 value = done/total*100（1 位小数，0-100）；total 缺失/为 0 → null（缺口）。
  function completionRateSeries(points) {
    return (points || []).map(function (p) {
      var s = p.summary || {};
      var total = s.total || 0;
      var done = s.done || 0;
      return {
        ts: p.ts,
        value: total > 0 ? Math.round(done / total * 1000) / 10 : null
      };
    });
  }

  // 事件速率序列（分桶）：按固定时间桶统计任务状态迁移次数 → 迁移次数/小时。
  // 相邻快照 i-1 → i：Σ|Δstatus|（六状态计数绝对差之和）/ 2 = 迁移次数，
  // 按相邻快照中点归入所在时间桶；每桶 value = 桶内迁移次数 / 桶小时数。
  // 相邻 summary 缺失或 Δt≤0 → 该对不归桶；Δt 超 2 倍桶长（采样缺口）→ 不归桶（跨缺口归集会误导）。
  // 桶内无迁移 → 0（采样范围内无变化是有意义的值，不是缺口）。
  // 返回 [{ts(桶起点), value}]；桶长由 pickBucketHours 从 1/2/3/6/12/24h 选取（目标 ≥20 桶）。
  function eventRateSeries(points) {
    var pts = points || [];
    if (!pts.length) return [];
    var t0 = pts[0].ts, t1 = pts[pts.length - 1].ts;
    if (!(t1 > t0)) return [];
    var windowHours = (t1 - t0) / 3600;
    var bucketHours = pickBucketHours(windowHours);
    var bucketSecs = bucketHours * 3600;
    var nBuckets = Math.max(1, Math.ceil((t1 - t0) / bucketSecs));
    var counts = new Array(nBuckets);
    for (var b = 0; b < nBuckets; b++) counts[b] = 0;

    for (var i = 1; i < pts.length; i++) {
      var prev = pts[i - 1].summary;
      var cur = pts[i].summary;
      if (!prev || !cur) continue;             // 相邻 summary 缺失 → 不归桶
      var dt = pts[i].ts - pts[i - 1].ts;
      if (!(dt > 0)) continue;                 // Δt≤0 → 不归桶
      if (dt > 2 * bucketSecs) continue;       // 采样缺口 → 不归桶
      var delta = 0;
      for (var k = 0; k < STATUS_KEYS.length; k++) {
        var st = STATUS_KEYS[k];
        delta += Math.abs((cur[st] || 0) - (prev[st] || 0));
      }
      var transitions = delta / 2;
      if (!transitions) continue;
      var mid = (pts[i - 1].ts + pts[i].ts) / 2;
      var bi = Math.floor((mid - t0) / bucketSecs);
      if (bi >= 0 && bi < nBuckets) counts[bi] += transitions;
    }

    var out = [];
    for (var b = 0; b < nBuckets; b++) {
      out.push({ ts: t0 + b * bucketSecs, value: Math.round(counts[b] / bucketHours * 10) / 10 });
    }
    return out;
  }

  // 桶长选取：从 1/2/3/6/12/24h 中选最大的使桶数 ≥20 的档（不足 20 桶时回退到 1h）。
  function pickBucketHours(windowHours) {
    var BUCKET_HOURS = [1, 2, 3, 6, 12, 24];
    var best = BUCKET_HOURS[0];
    for (var i = 0; i < BUCKET_HOURS.length; i++) {
      if (Math.ceil(windowHours / BUCKET_HOURS[i]) >= 20) best = BUCKET_HOURS[i];
      else break;
    }
    return best;
  }

  // 降采样：点数超 max 时等距取 max 个（保留首尾），控制 SVG 规模。
  function downsample(series, max) {
    var n = (series || []).length;
    if (n <= max) return (series || []).slice();
    var step = (n - 1) / (max - 1);
    var out = [];
    for (var i = 0; i < max; i++) {
      out.push(series[Math.round(i * step)]);
    }
    out[out.length - 1] = series[n - 1];
    return out;
  }

  // —— 格式化 ——

  function pad2(n) { return (n < 10 ? "0" : "") + n; }

  // mode: "time" → HH:MM；其他 → MM-DD
  function formatTime(ts, mode) {
    if (ts === null || ts === undefined) return "";
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return String(ts);
    if (mode === "time") return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
    return pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
  }

  // value → 字符串（整数原样，小数 1 位）+ unit（如 "%" / "/h"）；空值 → ""
  function formatY(value, unit) {
    if (value === null || value === undefined) return "";
    var v = Math.round(value * 10) / 10;
    return (v % 1 === 0 ? String(v) : v.toFixed(1)) + (unit || "");
  }

  // —— 缩放几何（纯计算，返回坐标/线段/网格/标签；由 main.js 转 SVG）——

  // opts: {width,height,padL,padR,padT,padB,yMin,yMax}；yMax 缺省时按数据自动 nice ceiling。
  // 返回 {width,height,padL,padT,padB,innerW,innerH,t0,t1,yMin,yMax,x,y,segments,gridlines,xLabels}
  // segments: [[{x,y,ts,value}], ...]（null 断开成多段）；gridlines: [{y,value}×5]；xLabels: [{x,ts}≤4]
  function scaleSeries(series, opts) {
    opts = opts || {};
    var width = opts.width || 560;
    var height = opts.height || 160;
    var padL = opts.padL != null ? opts.padL : 42;
    var padR = opts.padR != null ? opts.padR : 12;
    var padT = opts.padT != null ? opts.padT : 12;
    var padB = opts.padB != null ? opts.padB : 24;
    var innerW = width - padL - padR;
    var innerH = height - padT - padB;
    var yMin = opts.yMin != null ? opts.yMin : 0;
    var yMax = opts.yMax != null ? opts.yMax : 0;

    // 自动 y 上限：数据峰值 nice ceiling（0 起、至少 1）
    if (opts.yMax == null) {
      var maxVal = null;
      (series || []).forEach(function (p) {
        if (p.value !== null && p.value !== undefined && (maxVal === null || p.value > maxVal)) maxVal = p.value;
      });
      if (maxVal === null) maxVal = 0;
      yMax = maxVal > 0 ? niceCeil(maxVal) : 1;
    }
    if (yMax === yMin) yMax = yMin + 1;

    var t0 = series.length ? series[0].ts : 0;
    var t1 = series.length ? series[series.length - 1].ts : 0;
    if (t1 === t0) { t0 -= 1; t1 += 1; } // 单点居中

    function x(i) {
      var t = series[i] ? series[i].ts : t0;
      return padL + (t - t0) / (t1 - t0) * innerW;
    }
    function y(v) {
      if (v === null || v === undefined) return null;
      return padT + (1 - (v - yMin) / (yMax - yMin)) * innerH;
    }

    // 折线段：空值点断开
    var segments = [];
    var cur = null;
    for (var i = 0; i < series.length; i++) {
      var yy = y(series[i].value);
      if (yy === null) { cur = null; continue; }
      if (!cur) { cur = []; segments.push(cur); }
      cur.push({ x: x(i), y: yy, ts: series[i].ts, value: series[i].value });
    }

    // 网格线：5 条
    var gridlines = [];
    for (var g = 0; g < 5; g++) {
      gridlines.push({ y: padT + (1 - g / 4) * innerH, value: yMin + (yMax - yMin) * g / 4 });
    }

    // X 轴时间标签：最多 4 个（等距取实际点）
    var xLabels = [];
    var step = Math.max(1, Math.floor((series.length - 1) / 3));
    for (var idx = 0; idx < series.length && xLabels.length < 4; idx += step) {
      xLabels.push({ x: x(idx), ts: series[idx].ts });
    }
    if (series.length) {
      var last = xLabels[xLabels.length - 1];
      if (!last || last.ts !== series[series.length - 1].ts) {
        xLabels.push({ x: x(series.length - 1), ts: series[series.length - 1].ts });
      }
    }

    return {
      width: width, height: height,
      padL: padL, padT: padT, padB: padB,
      innerW: innerW, innerH: innerH,
      t0: t0, t1: t1, yMin: yMin, yMax: yMax,
      x: x, y: y, segments: segments,
      gridlines: gridlines, xLabels: xLabels
    };
  }

  // y 轴自动上限取整：10 的幂内取 1/1.5/2/3/5/10 档
  function niceCeil(v) {
    var mag = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var norm = v / mag;
    var nice;
    if (norm <= 1) nice = 1;
    else if (norm <= 1.5) nice = 1.5;
    else if (norm <= 2) nice = 2;
    else if (norm <= 3) nice = 3;
    else if (norm <= 5) nice = 5;
    else nice = 10;
    return nice * mag;
  }

  return {
    STATUS_KEYS: STATUS_KEYS,
    completionRateSeries: completionRateSeries,
    eventRateSeries: eventRateSeries,
    pickBucketHours: pickBucketHours,
    downsample: downsample,
    formatTime: formatTime,
    formatY: formatY,
    scaleSeries: scaleSeries,
    niceCeil: niceCeil
  };
});

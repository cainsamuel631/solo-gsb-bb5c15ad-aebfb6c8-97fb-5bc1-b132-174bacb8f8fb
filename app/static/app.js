/* 字幕断句校验台 — 原生前端（无框架） */
(function () {
  "use strict";

  var TYPE_LABELS = {
    overlong: "超字数",
    dwell: "停留过短",
    speed: "阅读速度过快",
    unsolvable: "不可满足",
    speed_warn: "接近上限",
  };

  var state = {
    analysisId: null,
    result: null,
    params: null,
    file: null,
  };

  function $(id) { return document.getElementById(id); }

  function fmtMs(ms) {
    var s = Math.max(0, ms) / 1000;
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    var pad = function (x, n) { return String(x).padStart(n, "0"); };
    return pad(h, 2) + ":" + pad(m, 2) + ":" + sec.toFixed(3).padStart(6, "0");
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  // ---------------------------------------------------------------- 上传
  var fileInput = $("file");
  fileInput.addEventListener("change", function () {
    state.file = fileInput.files[0] || null;
    $("file-label").textContent = state.file ? state.file.name : "点击选择 .vtt / .srt 文件…";
  });

  // 拖拽上传
  var picker = document.querySelector(".file-picker");
  ["dragenter", "dragover"].forEach(function (ev) {
    picker.addEventListener(ev, function (e) { e.preventDefault(); picker.classList.add("drag"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    picker.addEventListener(ev, function (e) { e.preventDefault(); picker.classList.remove("drag"); });
  });
  picker.addEventListener("drop", function (e) {
    var f = e.dataTransfer.files[0];
    if (f) {
      state.file = f;
      fileInput.files = e.dataTransfer.files;
      $("file-label").textContent = f.name;
    }
  });

  $("analyze-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (!state.file) {
      setStatus("请先选择字幕文件", true);
      return;
    }
    var fd = new FormData();
    fd.append("file", state.file);
    fd.append("max_chars", $("max_chars").value);
    fd.append("min_dwell_ms", $("min_dwell_ms").value);
    fd.append("cps_max", $("cps_max").value);
    fd.append("merge_gap_ms", $("merge_gap_ms").value);

    var btn = $("submit-btn");
    btn.disabled = true;
    btn.textContent = "分析中…";
    setStatus("正在解析并执行动态规划断句…");

    fetch("/api/analyze", { method: "POST", body: fd })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.detail || ("HTTP " + r.status)); });
        return r.json();
      })
      .then(function (summary) {
        return fetch("/api/analyses/" + summary.analysis_id).then(function (r) { return r.json(); })
          .then(function (detail) {
            state.analysisId = summary.analysis_id;
            state.result = Object.assign({}, detail, summary);
            renderResult(summary, detail);
          });
      })
      .catch(function (err) { setStatus(err.message, true); })
      .finally(function () {
        btn.disabled = false;
        btn.textContent = "开始校验";
      });
  });

  function setStatus(msg, isError) {
    var s = $("form-status");
    s.textContent = msg;
    s.className = "status" + (isError ? " error" : "");
  }

  // ---------------------------------------------------------------- 渲染
  function renderResult(summary, detail) {
    setStatus("");
    ["result-card", "issues-card", "cues-card"].forEach(function (id) {
      $(id).classList.remove("hidden");
    });

    $("lang-badge").textContent = "语言：" + summary.language_label;
    state.params = detail.params;

    var st = detail.stats;
    var stats = [
      { num: st.original_cue_count, cap: "原始 cue 数" },
      { num: st.revised_cue_count, cap: "修订后 cue 数" },
      { num: st.error_count, cap: "硬约束违规", cls: st.error_count ? "red" : "green" },
      { num: st.warning_count, cap: "接近阈值", cls: st.warning_count ? "amber" : "green" },
    ];
    var statBox = $("stats");
    statBox.innerHTML = "";
    stats.forEach(function (s) {
      var d = el("div", "stat");
      d.appendChild(el("div", "num " + (s.cls || ""), s.num));
      d.appendChild(el("div", "cap", s.cap));
      statBox.appendChild(d);
    });

    var exportBtn = $("export-btn");
    exportBtn.href = summary.export_url;
    exportBtn.classList.remove("hidden");

    renderCues(detail.cues);
    loadFilters();
  }

  function cueState(cue) {
    if (cue.issues.some(function (i) { return i.severity === "error"; })) return "error";
    if (cue.issues.length) return "warning";
    return "ok";
  }

  function renderCues(cues) {
    var box = $("cue-list");
    box.innerHTML = "";
    if (!cues.length) {
      box.appendChild(el("div", "empty", "无 cue"));
      return;
    }
    var p = state.params || { cps_max: Infinity, max_chars: Infinity };
    cues.forEach(function (cue) {
      var st = cueState(cue);
      var row = el("div", "cue " + st);

      var dur = cue.end_ms - cue.start_ms;
      var timeHtml =
        esc(fmtMs(cue.start_ms)) + "<br>→ " + esc(fmtMs(cue.end_ms)) +
        "<br>停留 <b>" + dur + "</b> ms";
      row.appendChild(el("div", "time", timeHtml));
      row.appendChild(el("div", "text", esc(cue.text)));

      var cpsCls = cue.cps > p.cps_max ? "bad" : cue.issues.length ? "near" : "";
      var lenCls = cue.max_line_len > p.max_chars ? "bad" : "";
      var metricsHtml =
        '<b class="' + cpsCls + '">' + cue.cps.toFixed(1) + "</b> 字/秒<br>" +
        '行长 <b class="' + lenCls + '">' + cue.max_line_len + "</b> 字";
      row.appendChild(el("div", "metrics", metricsHtml));

      if (cue.issues.length) {
        var box2 = el("div", "cue-issues");
        cue.issues.forEach(function (iss) {
          box2.appendChild(el("div", null, "• " + esc(iss.message)));
        });
        // 原因解释（detail 中）
        var detail = cueDetailFor(cue);
        if (detail && detail.reason_summary) {
          box2.appendChild(el("div", "reason", "原因：" + esc(detail.reason_summary)));
        }
        row.appendChild(box2);
      }
      box.appendChild(row);
    });
  }

  function cueDetailFor(cue) {
    if (!state.result || !state.result.issues) return null;
    var matches = state.result.issues.filter(function (i) {
      return i.cue_index === cue.index && i.severity === "error";
    });
    return matches.length ? matches[0].detail : null;
  }

  // ---------------------------------------------------------------- 筛选
  function loadFilters() {
    fetch("/api/languages").then(function (r) { return r.json(); }).then(function (data) {
      var sel = $("filter-language");
      var current = sel.value;
      sel.innerHTML = '<option value="all">全部</option>';
      data.languages.forEach(function (f) {
        var o = document.createElement("option");
        o.value = f.language;
        o.textContent = f.label + " (" + f.n + ")";
        sel.appendChild(o);
      });
      sel.value = current;
      loadIssues();
    }).catch(function () { loadIssues(); });
  }

  function loadIssues() {
    var lang = $("filter-language").value;
    var type = $("filter-type").value;
    fetch("/api/issues?language=" + encodeURIComponent(lang) + "&type=" + encodeURIComponent(type))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var box = $("issue-list");
        box.innerHTML = "";
        if (!data.issues.length) {
          box.appendChild(el("div", "empty", "没有匹配的问题 🎉"));
          return;
        }
        data.issues.forEach(function (iss) {
          var d = el("div", "issue " + iss.severity);
          d.appendChild(el(
            "div",
            "meta",
            '<span class="tag ' + iss.severity + '">' + esc(TYPE_LABELS[iss.type] || iss.type) + "</span>" +
            esc(iss.filename) + " · cue #" + (iss.cue_index + 1) + " · 任务 #" + iss.analysis_id
          ));
          d.appendChild(el("div", null, esc(iss.message)));
          if (iss.detail && iss.detail.reason_summary) {
            d.appendChild(el("div", "reason", "原因：" + esc(iss.detail.reason_summary)));
          }
          if (iss.detail && iss.detail.segment_text) {
            d.appendChild(el("span", "seg-text", "「" + esc(iss.detail.segment_text) + "」"));
          }
          box.appendChild(d);
        });
      });
  }

  $("filter-language").addEventListener("change", loadIssues);
  $("filter-type").addEventListener("change", loadIssues);
})();

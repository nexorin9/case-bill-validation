/* ============================================================================
 * 病案结算清单核验系统 · 浏览器面板脚本（原生 JS，无前端框架）
 * ----------------------------------------------------------------------------
 * 主路径：
 *   1. 上传表单 → POST /api/cases → 拿到 case_id
 *   2. 打开 EventSource('/api/cases/{id}/stream') 订阅进度
 *   3. step 事件 → 更新顶部进度条；field 事件 → 更新字段表行
 *   4. 「看依据」按钮 → 在原文区用 <mark> 高亮 span，3 秒后淡化
 *   5. 「确认」/「驳回」按钮 → POST /api/cases/{id}/fields/{field}/(confirm|reject)
 *   6. 「导出」按钮 → POST /api/cases/{id}/export
 * ============================================================================
 */

(function () {
  "use strict";

  // -------------------------------------------------------------------------
  // 工具
  // -------------------------------------------------------------------------

  /** 简短 toast 提示（替代原生 alert，避免浏览器默认样式）。 */
  function showToast(message, variant) {
    var existing = document.querySelector(".toast");
    if (existing) existing.remove();
    var toast = document.createElement("div");
    toast.className = "toast is-visible" + (variant ? " is-" + variant : "");
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(function () {
      toast.classList.remove("is-visible");
      setTimeout(function () { toast.remove(); }, 250);
    }, 2400);
  }

  /** 更新上传提示文本。 */
  function setUploadHint(text, isError) {
    var hint = document.getElementById("upload-hint");
    if (!hint) return;
    hint.textContent = text;
    hint.classList.toggle("is-error", Boolean(isError));
  }

  /** 在原文区定位 span → 用 mark 包裹 + 滚动到视野 + 3 秒后淡化。 */
  function scrollToSpan(rawText, startChar, endChar) {
    var pre = document.getElementById("source-text");
    if (!pre) return;
    if (typeof startChar !== "number" || typeof endChar !== "number"
        || endChar <= startChar || startChar < 0 || endChar > (rawText || pre.textContent).length) {
      // 越界兜底：仅滚动到顶部，提示无 span 可定位
      pre.scrollTop = 0;
      return;
    }
    var source = pre.textContent;
    var before = source.slice(0, startChar);
    var target = source.slice(startChar, endChar);
    var after = source.slice(endChar);

    // 清除旧的高亮
    var old = pre.querySelectorAll("mark.field-highlight");
    old.forEach(function (m) {
      var parent = m.parentNode;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });

    pre.textContent = "";
    pre.appendChild(document.createTextNode(before));
    var mark = document.createElement("mark");
    mark.className = "field-highlight";
    mark.textContent = target;
    pre.appendChild(mark);
    pre.appendChild(document.createTextNode(after));

    // 滚动到视野
    mark.scrollIntoView({ behavior: "smooth", block: "center" });

    // 3 秒后淡化
    setTimeout(function () {
      if (mark.isConnected) mark.classList.add("fading");
    }, 3000);
  }

  /** 7 态状态对应的中文标签。 */
  var STATUS_LABELS = {
    pending: "待抽取",
    extracting: "抽取中",
    rule_checked: "规则核",
    semantic_checked: "语义核",
    confirmed: "已确认",
    rejected: "已驳回",
    flagged: "冲突"
  };

  /** 流水线步骤中文映射。 */
  var STEP_LABELS = {
    segmenter: "段落切分",
    extractor: "字段抽取",
    cross_validator: "跨字段校验",
    rule_checker: "规则审查",
    semantic_checker: "语义核",
    done: "完成",
    error: "出错"
  };

  // -------------------------------------------------------------------------
  // 状态（单实例）
  // -------------------------------------------------------------------------

  var state = {
    caseId: null,
    doc: null,
    rawText: "",
    eventSource: null,
    processedSteps: new Set(),
    filterQuery: "",
    statusFilter: null  // null = 不过滤；字符串 = 只显示该状态
  };

  // -------------------------------------------------------------------------
  // 渲染：字段表
  // -------------------------------------------------------------------------

  /** 渲染字段表（首次打开或 case 切换时全量重绘）。 */
  function renderFieldTable() {
    var tbody = document.getElementById("field-table-body");
    tbody.innerHTML = "";
    if (!state.doc || !state.doc.extracted_fields) {
      var empty = document.createElement("tr");
      empty.className = "empty-row";
      empty.innerHTML = '<td colspan="5">尚无字段，等待流水线抽取完成…</td>';
      tbody.appendChild(empty);
      return;
    }
    var fields = state.doc.extracted_fields;
    var names = Object.keys(fields);
    if (names.length === 0) {
      var empty = document.createElement("tr");
      empty.className = "empty-row";
      empty.innerHTML = '<td colspan="5">字段表为空（解析失败或文件无文本）。</td>';
      tbody.appendChild(empty);
      return;
    }
    names.sort();
    names.forEach(function (name) {
      tbody.appendChild(buildFieldRow(name, fields[name]));
    });
    applyFilter();
    renderStatusCounts();
  }

  /** 单行字段。 */
  function buildFieldRow(fieldName, mark) {
    var tr = document.createElement("tr");
    tr.dataset.fieldName = fieldName;
    tr.dataset.status = mark.status;

    var tdName = document.createElement("td");
    tdName.className = "field-name";
    tdName.textContent = fieldName;

    var tdValue = document.createElement("td");
    tdValue.className = "field-value";
    tdValue.textContent = (mark.value === null || mark.value === undefined || mark.value === "")
      ? "（空）"
      : String(mark.value);

    var tdStatus = document.createElement("td");
    var badge = document.createElement("span");
    badge.className = "status-badge is-clickable";
    badge.dataset.status = mark.status;
    badge.textContent = STATUS_LABELS[mark.status] || mark.status;
    badge.title = "点击过滤同状态字段";
    badge.addEventListener("click", function () { toggleStatusFilter(mark.status); });
    tdStatus.appendChild(badge);

    var tdConf = document.createElement("td");
    tdConf.className = "field-confidence";
    var conf = (typeof mark.confidence === "number") ? mark.confidence.toFixed(2) : "—";
    tdConf.textContent = conf;

    var tdActions = document.createElement("td");
    tdActions.className = "field-actions";
    var btnSpan = document.createElement("button");
    btnSpan.type = "button";
    btnSpan.className = "btn btn--ghost";
    btnSpan.textContent = "看依据";
    btnSpan.disabled = !mark.span || typeof mark.span.start_char !== "number";
    btnSpan.addEventListener("click", function () { onShowEvidence(fieldName, mark); });
    tdActions.appendChild(btnSpan);

    var btnConfirm = document.createElement("button");
    btnConfirm.type = "button";
    btnConfirm.className = "btn btn--primary";
    btnConfirm.textContent = "确认";
    btnConfirm.disabled = !(mark.status === "rule_checked" || mark.status === "semantic_checked");
    btnConfirm.addEventListener("click", function () { onConfirm(fieldName); });
    tdActions.appendChild(btnConfirm);

    var btnReject = document.createElement("button");
    btnReject.type = "button";
    btnReject.className = "btn btn--danger";
    btnReject.textContent = "驳回";
    btnReject.disabled = !(mark.status === "rule_checked" || mark.status === "semantic_checked");
    btnReject.addEventListener("click", function () { onReject(fieldName); });
    tdActions.appendChild(btnReject);

    tr.appendChild(tdName);
    tr.appendChild(tdValue);
    tr.appendChild(tdStatus);
    tr.appendChild(tdConf);
    tr.appendChild(tdActions);
    return tr;
  }

  /** 单字段状态推进（更新行 + 计数）。 */
  function updateFieldRow(fieldName, nextStatus) {
    if (!state.doc || !state.doc.extracted_fields) return;
    var mark = state.doc.extracted_fields[fieldName];
    if (!mark) return;
    mark.status = nextStatus;

    var tr = document.querySelector('#field-table-body tr[data-field-name="' + cssEscape(fieldName) + '"]');
    if (!tr) {
      // 字段首次出现 → 重绘整张表
      renderFieldTable();
      return;
    }
    tr.dataset.status = nextStatus;
    var badge = tr.querySelector(".status-badge");
    if (badge) {
      badge.dataset.status = nextStatus;
      badge.textContent = STATUS_LABELS[nextStatus] || nextStatus;
    }
    // 重新评估按钮可用性
    var btns = tr.querySelectorAll(".btn");
    var enabled = (nextStatus === "rule_checked" || nextStatus === "semantic_checked");
    if (btns.length >= 2) {
      btns[1].disabled = !enabled;  // 确认
      btns[2].disabled = !enabled;  // 驳回
    }
    renderStatusCounts();
    applyFilter();
  }

  /** 状态计数（每个状态徽章 + 数量）。 */
  function renderStatusCounts() {
    var counts = {};
    Object.keys(STATUS_LABELS).forEach(function (s) { counts[s] = 0; });
    if (state.doc && state.doc.extracted_fields) {
      Object.keys(state.doc.extracted_fields).forEach(function (n) {
        var s = state.doc.extracted_fields[n].status;
        if (counts[s] === undefined) counts[s] = 0;
        counts[s] += 1;
      });
    }
    var host = document.getElementById("status-counts");
    host.innerHTML = "";
    Object.keys(counts).forEach(function (s) {
      if (counts[s] === 0) return;
      var b = document.createElement("span");
      b.className = "badge";
      b.dataset.status = s;
      b.style.cursor = "pointer";
      b.title = "点击过滤 " + (STATUS_LABELS[s] || s);
      b.textContent = (STATUS_LABELS[s] || s) + ": " + counts[s];
      b.addEventListener("click", function () { toggleStatusFilter(s); });
      host.appendChild(b);
    });
  }

  // -------------------------------------------------------------------------
  // 过滤
  // -------------------------------------------------------------------------

  /** 应用过滤（搜索 + 状态过滤）。 */
  function applyFilter() {
    var rows = document.querySelectorAll('#field-table-body tr[data-field-name]');
    var q = state.filterQuery.toLowerCase();
    rows.forEach(function (tr) {
      var name = tr.dataset.fieldName.toLowerCase();
      var value = (tr.querySelector(".field-value") || {}).textContent || "";
      value = value.toLowerCase();
      var status = tr.dataset.status;
      var passText = !q || name.indexOf(q) >= 0 || value.indexOf(q) >= 0;
      var passStatus = !state.statusFilter || state.statusFilter === status;
      tr.classList.toggle("filtered-out", !(passText && passStatus));
    });
  }

  function toggleStatusFilter(status) {
    if (state.statusFilter === status) {
      state.statusFilter = null;
    } else {
      state.statusFilter = status;
    }
    applyFilter();
  }

  /**
   * 搜索过滤（任务 28 显式契约）。
   * - 输入：用户输入框中的关键字（已 trim）
   * - 行为：写入 state.filterQuery → 触发 applyFilter（搜索 + 状态过滤）
   *        → DOM 中不匹配的行加 .filtered-out { display:none } 类
   *        → 顶部状态计数同步刷新（renderStatusCounts 已在 applyFilter 内重渲）
   */
  function filterRows(query) {
    state.filterQuery = (query == null ? "" : String(query)).trim();
    applyFilter();
  }

  /**
   * 状态徽章点击过滤（任务 28 显式契约）。
   * - 输入：状态名（pending / extracting / rule_checked / semantic_checked /
   *        confirmed / rejected / flagged）
   * - 行为：与当前 state.statusFilter 比较；相同 → 清空过滤；不同 → 仅显示该状态行
   *        → 顶部状态计数同步刷新
   */
  function statusBadgeClick(status) {
    toggleStatusFilter(status);
  }

  // -------------------------------------------------------------------------
  // 进度条
  // -------------------------------------------------------------------------

  function updateProgressStep(stepKey) {
    var label = STEP_LABELS[stepKey] || stepKey;
    var stepEl = document.getElementById("progress-step");
    if (stepEl) stepEl.textContent = label;

    if (stepKey === "done") {
      document.querySelectorAll(".progress-step-item").forEach(function (li) {
        li.classList.add("is-done");
        li.classList.remove("is-active");
      });
      return;
    }

    document.querySelectorAll(".progress-step-item").forEach(function (li) {
      var key = li.dataset.step;
      if (key === stepKey) {
        li.classList.add("is-active");
        li.classList.remove("is-done");
      } else if (state.processedSteps.has(key)) {
        li.classList.add("is-done");
        li.classList.remove("is-active");
      } else {
        li.classList.remove("is-active", "is-done");
      }
    });
    if (PIPELINE_STEP_ORDER.indexOf(stepKey) >= 0) {
      state.processedSteps.add(stepKey);
    }
  }

  var PIPELINE_STEP_ORDER = ["segmenter", "extractor", "cross_validator", "rule_checker", "semantic_checker"];

  // -------------------------------------------------------------------------
  // 操作
  // -------------------------------------------------------------------------

  function onShowEvidence(fieldName, mark) {
    if (!mark.span || typeof mark.span.start_char !== "number") {
      showToast("字段 " + fieldName + " 没有原文依据", "danger");
      return;
    }
    scrollToSpan(state.rawText, mark.span.start_char, mark.span.end_char);
    var rows = document.querySelectorAll('#field-table-body tr[data-field-name]');
    rows.forEach(function (r) { r.classList.remove("is-selected"); });
    var tr = document.querySelector(
      '#field-table-body tr[data-field-name="' + cssEscape(fieldName) + '"]'
    );
    if (tr) tr.classList.add("is-selected");
  }

  function onConfirm(fieldName) {
    if (!state.caseId) return;
    postFieldAction("/confirm", fieldName);
  }

  function onReject(fieldName) {
    if (!state.caseId) return;
    postFieldAction("/reject", fieldName);
  }

  function postFieldAction(action, fieldName) {
    var url = "/api/cases/" + encodeURIComponent(state.caseId)
            + "/fields/" + encodeURIComponent(fieldName) + action;
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({})
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        updateFieldRow(fieldName, data.status);
        showToast("字段 " + fieldName + " 已" + (action === "/confirm" ? "确认" : "驳回"), "success");
      })
      .catch(function (err) {
        showToast("操作失败：" + err.message, "danger");
      });
  }

  function onExport() {
    if (!state.caseId) return;
    var btn = document.getElementById("export-btn");
    var status = document.getElementById("export-status");
    btn.disabled = true;
    status.textContent = "正在导出…";
    fetch("/api/cases/" + encodeURIComponent(state.caseId) + "/export", { method: "POST" })
      .then(function (resp) {
        if (resp.status === 501) throw new Error("导出模块尚未接入");
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        status.textContent = "已导出：" + data.excel_path;
        showToast("导出成功", "success");
      })
      .catch(function (err) {
        status.textContent = "导出失败：" + err.message;
        showToast("导出失败：" + err.message, "danger");
      })
      .finally(function () { btn.disabled = false; });
  }

  // -------------------------------------------------------------------------
  // SSE
  // -------------------------------------------------------------------------

  function openStream(caseId) {
    closeStream();
    var url = "/api/cases/" + encodeURIComponent(caseId) + "/stream";
    if (typeof EventSource === "undefined") {
      showToast("浏览器不支持 EventSource", "danger");
      return;
    }
    var es = new EventSource(url);
    state.eventSource = es;
    // 重连计数：客户端 UI 上给一个直观的「等待重连」指示
    state.reconnectAttempts = 0;
    state.streamConnected = true;

    es.onopen = function () {
      // 首次连接成功 / 自动重连成功都走这里
      state.reconnectAttempts = 0;
      state.streamConnected = true;
      hideReconnectingBanner();
      // 重连成功后清掉旧 toast，避免堆叠
      var old = document.querySelector(".toast.is-reconnect");
      if (old) old.remove();
    };

    es.onmessage = function (evt) {
      var payload;
      try { payload = JSON.parse(evt.data); } catch (e) { return; }
      handleSseEvent(payload);
    };
    es.onerror = function () {
      // EventSource 默认会自动重连（readyState === CLOSED 后不会重连，
      // 但 readyState === CONNECTING 时浏览器按 retry 间隔重连）；我们
      // 在 UI 上提示 + 累计重连次数，超过 5 次升级为 toast 提示。
      state.streamConnected = false;
      state.reconnectAttempts += 1;
      showReconnectingBanner(state.reconnectAttempts);
      if (state.reconnectAttempts > 5) {
        showToast(
          "SSE 连接已断开 " + state.reconnectAttempts + " 次，浏览器仍在尝试重连…",
          "danger"
        );
      }
    };
  }

  /**
   * 展示「连接中断，正在重连…」持久化提示横幅（区别于短 toast）。
   * 用 document.body 上一根 .reconnecting-banner 元素承载，重连成功由
   * hideReconnectingBanner 摘掉。
   */
  function showReconnectingBanner(attempt) {
    var banner = document.getElementById("reconnecting-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "reconnecting-banner";
      banner.className = "reconnecting-banner is-visible";
      banner.innerHTML =
        '<span class="reconnecting-banner__dot"></span>' +
        '<span class="reconnecting-banner__text">连接中断，正在自动重连…</span>';
      document.body.appendChild(banner);
    }
    var textEl = banner.querySelector(".reconnecting-banner__text");
    if (textEl) {
      textEl.textContent = attempt > 1
        ? "连接中断，正在自动重连（第 " + attempt + " 次）…"
        : "连接中断，正在自动重连…";
    }
    banner.classList.add("is-visible");
  }

  function hideReconnectingBanner() {
    var banner = document.getElementById("reconnecting-banner");
    if (banner) banner.classList.remove("is-visible");
  }

  function closeStream() {
    if (state.eventSource) {
      state.eventSource.close();
      state.eventSource = null;
    }
  }

  function handleSseEvent(evt) {
    if (!evt) return;
    if (evt.step) {
      updateProgressStep(evt.step);
      if (evt.step === "done") {
        refreshDoc();
      }
      return;
    }
    if (evt.field && evt.status) {
      updateFieldRow(evt.field, evt.status);
    }
    if (evt.case_id && !state.caseId) {
      state.caseId = evt.case_id;
    }
  }

  // -------------------------------------------------------------------------
  // 数据加载
  // -------------------------------------------------------------------------

  function refreshDoc() {
    if (!state.caseId) return;
    fetch("/api/cases/" + encodeURIComponent(state.caseId))
      .then(function (r) { return r.json(); })
      .then(function (doc) {
        state.doc = doc;
        state.rawText = doc.raw_text || "";
        renderSource();
        renderFieldTable();
        var ps = doc.processed_steps || [];
        ps.forEach(function (s) { state.processedSteps.add(s); });
        ps.forEach(function (s) { updateProgressStep(s); });
        if (doc.pipeline_state === "confirmed" || doc.pipeline_state === "exported"
            || doc.pipeline_state === "submitted") {
          updateProgressStep("done");
        }
        document.getElementById("export-section").hidden = false;
        document.getElementById("main-grid").hidden = false;
      })
      .catch(function (err) {
        showToast("拉取文档失败：" + err.message, "danger");
      });
  }

  function renderSource() {
    var pre = document.getElementById("source-text");
    pre.textContent = state.rawText;
    var meta = document.getElementById("source-meta");
    meta.textContent = "原文长度：" + state.rawText.length + " 字";
  }

  // -------------------------------------------------------------------------
  // 上传
  // -------------------------------------------------------------------------

  function onUploadSubmit(ev) {
    ev.preventDefault();
    var form = ev.target;
    var fileInput = form.querySelector("#file-input");
    if (!fileInput.files || fileInput.files.length === 0) {
      setUploadHint("请选择文件后再上传。", true);
      return;
    }
    var file = fileInput.files[0];
    var fd = new FormData();
    fd.append("file", file);
    var btn = document.getElementById("upload-submit");
    btn.disabled = true;
    setUploadHint("上传中…");

    fetch("/api/cases", { method: "POST", body: fd })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        state.caseId = data.case_id;
        document.getElementById("case-id-display").textContent = "case_id: " + data.case_id;
        document.getElementById("pipeline-progress").hidden = false;
        document.getElementById("export-section").hidden = false;
        document.getElementById("main-grid").hidden = false;
        setUploadHint("已上传：" + (file.name || "未命名") + "（流水线已启动）");
        showToast("上传成功，流水线已启动", "success");
        openStream(data.case_id);
        // 不等 SSE，先拉一次 doc 拿到原文
        setTimeout(refreshDoc, 200);
      })
      .catch(function (err) {
        setUploadHint("上传失败：" + err.message, true);
        showToast("上传失败：" + err.message, "danger");
      })
      .finally(function () { btn.disabled = false; });
  }

  // -------------------------------------------------------------------------
  // 工具：CSS escape（用于 selector 拼接）
  // -------------------------------------------------------------------------

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_-]/g, function (c) {
      return "\\" + c;
    });
  }

  // -------------------------------------------------------------------------
  // 启动
  // -------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("upload-form");
    form.addEventListener("submit", onUploadSubmit);

    var filterInput = document.getElementById("filter-input");
    filterInput.addEventListener("input", function (e) {
      state.filterQuery = (e.target.value || "").trim();
      applyFilter();
    });

    var exportBtn = document.getElementById("export-btn");
    exportBtn.addEventListener("click", onExport);
  });
})();

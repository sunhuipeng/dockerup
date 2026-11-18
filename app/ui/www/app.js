/* 服务管理 - 前端逻辑 */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    boot: null,
    filter: "all",
    q: "",
    taskId: null,
    taskOffset: 0,
    taskTimer: null,
    taskTitle: "",
    logsProject: "",
    logsContainer: "",
    ymlProject: "",
    autoTimer: null,
    busy: {}
  };

  var ACTION_TEXT = {
    upgrade: "升级",
    pull: "拉取镜像",
    up: "启动",
    down: "停止",
    restart: "重启"
  };

  /* ---------------- 基础工具 ---------------- */

  function esc(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function request(url, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {} };
    if (options.body) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch(url, init).then(function (resp) {
      return resp.json().catch(function () {
        throw new Error("服务返回了无法识别的响应（HTTP " + resp.status + "）");
      }).then(function (data) {
        if (!resp.ok || data.ok === false) {
          throw new Error(data && data.error ? data.error : "请求失败（HTTP " + resp.status + "）");
        }
        return data;
      });
    });
  }

  function toast(message, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = message;
    $("toastWrap").appendChild(el);
    setTimeout(function () {
      el.style.opacity = "0";
      el.style.transform = "translateX(16px)";
      el.style.transition = "all .25s";
      setTimeout(function () { el.remove(); }, 260);
    }, kind === "err" ? 5200 : 3200);
  }

  /* ---------------- 加载与渲染 ---------------- */

  function load(silent) {
    if (!silent) {
      $("basePath").textContent = "正在加载…";
    }
    return request("api/bootstrap")
      .then(function (data) {
        state.boot = data;
        render();
      })
      .catch(function (err) {
        $("basePath").textContent = "无法连接后端服务";
        $("projectGrid").innerHTML = "";
        showEmpty("连接失败", err.message + "。请尝试在应用中心停止后重新启动本应用。", false);
      });
  }

  function render() {
    var boot = state.boot;
    if (!boot) return;

    var scan = boot.scan;
    var projects = scan.projects || [];

    $("basePath").textContent = scan.base_path + (scan.exists ? "" : " · 目录不存在");
    $("footerInfo").textContent = "服务管理 v" + boot.version;
    $("footerTime").textContent = "更新于 " + boot.server_time;

    var total = projects.length;
    var withCompose = projects.filter(function (p) { return p.has_compose; });
    var runningContainers = projects.reduce(function (sum, p) { return sum + (p.running || 0); }, 0);
    var totalContainers = projects.reduce(function (sum, p) { return sum + (p.total || 0); }, 0);

    $("statProjects").textContent = total;
    $("statProjectsSub").textContent = withCompose.length + " 个包含 compose 配置";

    $("statRunning").textContent = runningContainers;
    $("statRunningSub").textContent = "共 " + totalContainers + " 个容器";

    var docker = boot.docker || {};
    if (docker.ok) {
      $("statDocker").textContent = "正常";
      $("statDockerSub").textContent = "compose " + (docker.compose_version || "?") +
        (docker.docker_version ? " · docker " + docker.docker_version : "");
    } else {
      $("statDocker").textContent = "不可用";
      $("statDockerSub").textContent = docker.socket_ok ? "未找到 docker compose" : "未检测到 Docker 服务";
    }

    $("btnUpgradeAll").disabled = !docker.ok || withCompose.length === 0;

    renderList();
  }

  var UNIT_SEC = {
    second: 1,
    minute: 60,
    hour: 3600,
    day: 86400,
    week: 604800,
    month: 2592000,
    year: 31536000
  };

  /* 从英文 Status 估算运行秒数（仅用于排序），如 "Up About a minute"、"Up 3 days" */
  function upSeconds(status) {
    var m = String(status || "").match(/^up\s+(.+)$/i);
    if (!m) return -1;
    var d = m[1].toLowerCase();
    if (d.indexOf("less than a second") >= 0) return 0;
    var about = d.match(/about (?:a|an) (second|minute|hour|day|week|month|year)/);
    if (about) return UNIT_SEC[about[1]] || 0;
    var num = d.match(/(\d+)\s*(second|minute|hour|day|week|month|year)s?/);
    if (!num) return -1;
    return parseInt(num[1], 10) * (UNIT_SEC[num[2]] || 1);
  }

  /* 项目内运行中容器的最长运行秒数，无运行中容器返回 -1 */
  function projectRunSeconds(p) {
    var max = -1;
    (p.containers || []).forEach(function (c) {
      if (c.state !== "running") return;
      var sec = upSeconds(c.status);
      if (sec > max) max = sec;
    });
    return max;
  }

  function filteredProjects() {
    var scan = state.boot.scan;
    var list = (scan.projects || []).slice();

    if (state.filter === "running") {
      list = list.filter(function (p) { return p.has_compose && p.running > 0; });
    } else if (state.filter === "stopped") {
      list = list.filter(function (p) { return p.has_compose && !p.running; });
    } else if (state.filter === "invalid") {
      list = list.filter(function (p) { return !p.has_compose; });
    }

    if (state.q) {
      var q = state.q.toLowerCase();
      list = list.filter(function (p) {
        return p.name.toLowerCase().indexOf(q) >= 0 || (p.path || "").toLowerCase().indexOf(q) >= 0;
      });
    }

    // 排序：运行中的排前面，运行时间短的在前；未运行的统一排在后面（按名称）
    list.sort(function (a, b) {
      var ra = a.has_compose && a.running > 0 ? 1 : 0;
      var rb = b.has_compose && b.running > 0 ? 1 : 0;
      if (ra !== rb) return rb - ra;
      if (ra) {
        var sa = projectRunSeconds(a);
        var sb = projectRunSeconds(b);
        // 拿不到时长的（-1）排在有时长的之后
        if (sa < 0 && sb >= 0) return 1;
        if (sb < 0 && sa >= 0) return -1;
        if (sa !== sb) return sa - sb;
      }
      return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
    });

    return list;
  }

  function renderList() {
    var scan = state.boot.scan;
    var grid = $("projectGrid");
    var list = filteredProjects();

    if (!scan.exists) {
      grid.innerHTML = "";
      showEmpty("根目录不存在", "当前配置的项目根目录是 " + scan.base_path +
        "，但它不存在。请点击上方「设置」修改为实际的 Docker 项目目录。", true);
      return;
    }

    if (!scan.projects.length) {
      grid.innerHTML = "";
      showEmpty("没有找到任何项目", "目录 " + scan.base_path +
        " 下没有子文件夹。请把 Compose 项目放在该目录下，或修改根目录。", true);
      return;
    }

    if (!list.length) {
      grid.innerHTML = "";
      showEmpty("没有匹配的项目", "换个筛选条件或搜索关键词试试。", false);
      return;
    }

    $("emptyState").hidden = true;
    grid.innerHTML = list.map(buildCard).join("");

    // 事件委托
    if (!grid.dataset.bound) {
      grid.dataset.bound = "1";
      grid.addEventListener("click", onGridClick);
    }
  }

  function showEmpty(title, desc, showSettings) {
    var el = $("emptyState");
    el.hidden = false;
    el.innerHTML =
      '<div class="empty-icon">▤</div>' +
      "<h3>" + esc(title) + "</h3>" +
      "<p>" + esc(desc) + "</p>" +
      (showSettings ? '<button class="btn btn-primary" data-act="open-settings">打开设置</button>' : "");
  }

  function statusOf(p) {
    if (!p.has_compose) return { cls: "invalid", badge: "badge-warn", text: "无 compose 配置" };
    if (!p.total) return { cls: "stopped", badge: "badge-stopped", text: "无容器" };
    if (p.unmanaged) {
      return { cls: "running", badge: "badge-running", text: "未纳管 " + p.running + "/" + p.total };
    }
    if (p.running === p.total) return { cls: "running", badge: "badge-running", text: "运行中 " + p.running + "/" + p.total };
    if (p.running > 0) return { cls: "running", badge: "badge-running", text: "部分运行 " + p.running + "/" + p.total };
    return { cls: "stopped", badge: "badge-stopped", text: "已停止" };
  }

  function buildCard(p) {
    var st = statusOf(p);
    var busy = state.busy[p.name];

    var containers = (p.containers || []).slice(0, 4).map(function (c) {
      return '<div class="container-row">' +
        '<i class="c-dot ' + esc(c.state) + '"></i>' +
        '<span class="c-name" title="' + esc(c.image) + '">' + esc(c.name || c.service) + "</span>" +
        '<span class="c-status">' + esc(shortStatus(c.status, c.state)) + "</span>" +
        "</div>";
    }).join("");

    if ((p.containers || []).length > 4) {
      containers += '<div class="c-more">还有 ' + (p.containers.length - 4) + " 个容器…</div>";
    }

    var note = "";
    if (!p.has_compose) {
      note = '<div class="card-note">该目录下没有 docker-compose.yml / compose.yml，不会被当作项目处理。</div>';
    } else if (p.state_error) {
      note = '<div class="card-note">状态查询失败：' + esc(p.state_error) + "</div>";
    } else if (p.unmanaged) {
      note = '<div class="card-note">容器由 Docker 管理器或旧配置创建，不在本 Compose 项目内。升级时会自动删除同名旧容器并重建。</div>';
    }

    var actions = "";
    if (p.has_compose) {
      actions += '<button class="btn btn-soft btn-sm" data-act="upgrade" data-project="' + esc(p.name) + '"' +
        (busy ? " disabled" : "") + ">升级</button>";
      if (p.running > 0) {
        actions += '<button class="btn btn-ghost btn-sm" data-act="down" data-project="' + esc(p.name) + '"' +
          (busy ? " disabled" : "") + ">停止</button>";
      } else {
        actions += '<button class="btn btn-ghost btn-sm" data-act="up" data-project="' + esc(p.name) + '"' +
          (busy ? " disabled" : "") + ">启动</button>";
      }
      actions += '<button class="btn btn-ghost btn-sm" data-act="restart" data-project="' + esc(p.name) + '"' +
        (busy ? " disabled" : "") + ">重启</button>";
      actions += '<button class="btn btn-ghost btn-sm" data-act="yml" data-project="' + esc(p.name) +
        '" title="查看 compose 配置文件">YML</button>';
      actions += '<span class="spacer"></span>';
      actions += '<button class="btn btn-ghost btn-sm" data-act="logs" data-project="' + esc(p.name) + '">日志</button>';
    } else {
      actions = '<button class="btn btn-ghost btn-sm" data-act="open-settings">调整根目录</button>';
    }

    return '<article class="card is-' + st.cls + '">' +
      '<div class="card-head">' +
        '<div style="min-width:0">' +
          '<h3 class="card-title" data-copy="' + esc(p.path) + '" title="' + esc(p.path) + '（点击复制路径）">' +
            esc(p.name) + "</h3>" +
        "</div>" +
        '<span class="badge ' + st.badge + '"><i class="badge-dot"></i>' + esc(st.text) + "</span>" +
      "</div>" +
      (p.compose_file ? '<div class="chips"><span class="chip">' + esc(p.compose_file) + "</span>" +
        (p.total ? '<span class="chip">' + p.total + " 容器</span>" : "") + "</div>" : "") +
      note +
      (containers ? '<div class="containers">' + containers + "</div>" : "") +
      '<div class="card-actions">' + actions + "</div>" +
      "</article>";
  }

  var STATE_TEXT = {
    running: "运行中",
    exited: "已退出",
    created: "已创建",
    restarting: "重启中",
    paused: "已暂停",
    dead: "已停止",
    removing: "删除中"
  };

  var UNIT_CN = {
    second: "秒",
    minute: "分钟",
    hour: "小时",
    day: "天",
    week: "周",
    month: "个月",
    year: "年"
  };

  function unitCn(unit) {
    return UNIT_CN[String(unit).toLowerCase()] || unit;
  }

  /* 把 docker 的英文时长转中文，并去掉健康状态标记 */
  function cnDuration(text) {
    return String(text)
      .replace(/\((?:healthy|unhealthy|health:\s*[a-z]+)\)/gi, " ")
      .replace(/\bAbout (?:a|an) (second|minute|hour|day|week|month|year)s?\b/gi, function (m, u) {
        return "约 1 " + unitCn(u);
      })
      .replace(/\bLess than a second\b/gi, "不到 1 秒")
      .replace(/\b(\d+)\s*(second|minute|hour|day|week|month|year)s?\b/gi, function (m, n, u) {
        return n + " " + unitCn(u);
      })
      .replace(/\s+/g, " ")
      .trim();
  }

  function shortStatus(status, s) {
    var fallback = STATE_TEXT[s] || s || "";
    if (!status) return fallback;

    var text = String(status).replace(/\s+/g, " ").trim();

    // Up 2 hours (healthy) / Up About a minute
    if (/^up\b/i.test(text)) {
      var up = cnDuration(text.replace(/^up\b\s*/i, ""));
      return up ? "运行 " + up : "运行中";
    }

    // Exited (0) 2 hours ago / Exited (143) 44 hours ago
    if (/^exited\b/i.test(text)) {
      var ex = cnDuration(text.replace(/^exited\b\s*/i, "").replace(/^\(\d+\)\s*/, "").replace(/\s*ago$/i, ""));
      return ex ? "已退出 " + ex : "已退出";
    }

    // Restarting (1) 5 seconds ago
    if (/^restarting\b/i.test(text)) {
      var rs = cnDuration(text.replace(/^restarting\b\s*/i, "").replace(/^\(\d+\)\s*/, "").replace(/\s*ago$/i, ""));
      return rs ? "重启中 " + rs : "重启中";
    }

    if (/^created\b/i.test(text)) return "已创建";
    if (/^paused\b/i.test(text)) return "已暂停";
    if (/^dead\b/i.test(text)) return "已停止";

    // 未知格式：仅翻译状态词并去掉健康标记
    return cnDuration(
      text
        .replace(/\bUp\b/, "运行")
        .replace(/\bExited\b/i, "已退出")
        .replace(/\bCreated\b/i, "已创建")
        .replace(/\bRestarting\b/i, "重启中")
        .replace(/\bPaused\b/i, "已暂停")
        .replace(/\s*ago$/i, "")
    ) || fallback;
  }

  /* ---------------- 交互 ---------------- */

  function onGridClick(event) {
    var copyEl = event.target.closest("[data-copy]");
    if (copyEl) {
      copyPath(copyEl.getAttribute("data-copy"));
      return;
    }

    var btn = event.target.closest("[data-act]");
    if (!btn) return;

    var act = btn.getAttribute("data-act");
    var project = btn.getAttribute("data-project");

    if (act === "open-settings") return openSettings();
    if (act === "logs") return openLogs(project);
    if (act === "yml") return openYml(project);
    if (act === "down") {
      confirmDialog("停止项目", "将执行 docker compose down，停止并移除「" + project +
        "」的所有容器（不会删除数据卷）。确定继续？").then(function (yes) {
        if (yes) runAction(project, "down");
      });
      return;
    }
    if (act === "upgrade") {
      confirmDialog("升级项目", "「" + project + "」将依次执行：停止容器 → 拉取最新镜像 → 重新启动。\n\n期间服务会短暂中断，确定继续？")
        .then(function (yes) { if (yes) runAction(project, "upgrade"); });
      return;
    }
    runAction(project, act);
  }

  function copyPath(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast("路径已复制", "ok");
      }).catch(function () { toast("复制失败：" + text); });
    } else {
      toast(text);
    }
  }

  function runAction(project, action) {
    state.busy[project] = true;
    renderList();
    request("api/action", { method: "POST", body: { project: project, action: action } })
      .then(function (data) {
        openTask(data.task_id, project + " · " + (ACTION_TEXT[action] || action));
      })
      .catch(function (err) {
        delete state.busy[project];
        renderList();
        toast(err.message, "err");
      });
  }

  function upgradeAll() {
    var names = (state.boot.scan.projects || [])
      .filter(function (p) { return p.has_compose; })
      .map(function (p) { return p.name; });

    if (!names.length) return toast("没有可升级的项目", "err");

    confirmDialog("全部升级", "将对 " + names.length + " 个项目依次执行「停止 → 拉取镜像 → 启动」。\n\n" +
      "耗时可能较长，确定开始？").then(function (yes) {
      if (!yes) return;
      request("api/action", { method: "POST", body: { action: "upgrade", projects: names } })
        .then(function (data) { openTask(data.task_id, "批量升级 · " + names.length + " 个项目"); })
        .catch(function (err) { toast(err.message, "err"); });
    });
  }

  /* ---------------- 任务日志 ---------------- */

  function openTask(taskId, title) {
    if (state.taskTimer) clearTimeout(state.taskTimer);
    state.taskId = taskId;
    state.taskOffset = 0;
    state.taskTitle = title || "任务";

    $("taskDrawer").hidden = false;
    $("terminal").innerHTML = "";
    $("taskTitle").textContent = title || "任务";
    $("taskSub").textContent = "正在启动…";
    $("taskSpinner").className = "spinner";

    pollTask();
  }

  function pollTask() {
    if (!state.taskId) return;
    var id = state.taskId;

    request("api/task?id=" + encodeURIComponent(id) + "&offset=" + state.taskOffset)
      .then(function (data) {
        if (state.taskId !== id) return;
        var task = data.task;
        if (!task) {
          $("taskSub").textContent = "任务已过期";
          return;
        }

        appendLines(task.lines, task.total);
        state.taskOffset = task.next;

        if (task.running) {
          $("taskSub").textContent = "执行中 · 已运行 " + task.elapsed + " 秒";
          state.taskTimer = setTimeout(pollTask, 800);
        } else {
          $("taskTitle").textContent = task.title;
          $("taskSub").textContent = (task.success ? "已完成" : "执行失败（退出码 " + task.exit_code + "）") +
            " · 用时 " + task.elapsed + " 秒";
          $("taskSpinner").className = "spinner " + (task.success ? "done-ok" : "done-err");
          state.busy = {};
          load(true);
          if (!task.success) toast("操作未完全成功，请查看日志", "err");
        }
      })
      .catch(function (err) {
        $("taskSub").textContent = "获取日志失败：" + err.message;
        state.taskTimer = setTimeout(pollTask, 2000);
      });
  }

  function appendLines(lines, total) {
    var term = $("terminal");
    if (!lines || !lines.length) return;

    var atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 60;
    var frag = document.createDocumentFragment();
    lines.forEach(function (line) {
      var span = document.createElement("span");
      span.className = "ln " + (line.k || "out");
      span.textContent = line.t;
      frag.appendChild(span);
      frag.appendChild(document.createTextNode("\n"));
    });
    term.appendChild(frag);

    if (atBottom) term.scrollTop = term.scrollHeight;
  }

  /* ---------------- 日志对话框 ---------------- */

  function findProject(name) {
    var projects = state.boot && state.boot.scan ? state.boot.scan.projects || [] : [];
    for (var i = 0; i < projects.length; i++) {
      if (projects[i].name === name) return projects[i];
    }
    return null;
  }

  function openLogs(project) {
    state.logsProject = project;
    state.logsContainer = "";
    $("logsTitle").textContent = project + " · 容器日志";
    $("logsContent").textContent = "加载中…";

    // 多容器时显示选择器，让用户挑要看哪个容器的日志
    var p = findProject(project);
    var containers = (p && p.containers) || [];
    var picker = $("logsPicker");
    var select = $("logsContainer");

    if (containers.length > 1) {
      select.innerHTML = containers.map(function (c) {
        var value = c.name || c.service;
        var label = c.name || c.service;
        var stateText = c.state === "running" ? "运行中" : (STATE_TEXT[c.state] || c.state || "");
        return '<option value="' + esc(value) + '">' +
          esc(label) + (stateText ? "（" + stateText + "）" : "") + "</option>";
      }).join("");
      state.logsContainer = select.value || "";
      picker.hidden = false;
    } else {
      select.innerHTML = "";
      picker.hidden = true;
    }

    showModal("logsModal");
    fetchLogs();
  }

  function fetchLogs() {
    if (!state.logsProject) return;
    $("logsContent").textContent = "加载中…";
    var url = "api/logs?project=" + encodeURIComponent(state.logsProject) + "&tail=300";
    if (state.logsContainer) {
      url += "&container=" + encodeURIComponent(state.logsContainer);
    }
    request(url)
      .then(function (data) {
        $("logsContent").textContent = data.logs && data.logs.trim() ? data.logs : "（暂无日志输出）";
      })
      .catch(function (err) {
        $("logsContent").textContent = "读取失败：" + err.message;
      });
  }

  /* ---------------- Compose 配置查看 ---------------- */

  function openYml(project) {
    state.ymlProject = project;
    $("ymlTitle").textContent = project + " · Compose 配置";
    $("ymlContent").textContent = "加载中…";
    $("ymlPath").textContent = "";
    showModal("ymlModal");
    fetchYml();
  }

  function fetchYml() {
    if (!state.ymlProject) return;
    $("ymlContent").textContent = "加载中…";
    request("api/compose?project=" + encodeURIComponent(state.ymlProject))
      .then(function (data) {
        $("ymlTitle").textContent = state.ymlProject + " · " + (data.file || "compose 配置");
        $("ymlPath").textContent = data.path || "";
        $("ymlContent").textContent = data.content && data.content.trim() ? data.content : "（文件为空）";
      })
      .catch(function (err) {
        $("ymlContent").textContent = "读取失败：" + err.message;
        $("ymlPath").textContent = "";
      });
  }

  /* ---------------- 设置 ---------------- */

  function openSettings() {
    var path = state.boot && state.boot.scan ? state.boot.scan.base_path : "";
    $("inputBasePath").value = path;
    $("settingsAlert").hidden = true;
    $("detectBox").innerHTML = "";
    showModal("settingsModal");

    request("api/detect-base").then(function (data) {
      var paths = data.paths || [];
      if (!paths.length) {
        $("detectBox").innerHTML = '<div class="detect-title">未在常见位置自动发现 Docker 项目目录，请手动填写。</div>';
        return;
      }
      $("detectBox").innerHTML = '<div class="detect-title">检测到可能的目录（点击填入）：</div>' +
        paths.map(function (item) {
          return '<div class="detect-item" data-path="' + esc(item.path) + '">' +
            "<code>" + esc(item.path) + "</code><span>" + item.projects + " 个项目</span></div>";
        }).join("");
    }).catch(function () {
      $("detectBox").innerHTML = "";
    });
  }

  function saveSettings() {
    var value = $("inputBasePath").value.trim();
    var alertBox = $("settingsAlert");
    if (!value) {
      alertBox.className = "alert alert-error";
      alertBox.textContent = "请填写目录路径";
      alertBox.hidden = false;
      return;
    }
    $("btnSaveSettings").disabled = true;
    request("api/settings", { method: "POST", body: { base_path: value } })
      .then(function () {
        hideModal("settingsModal");
        toast("已保存，正在重新扫描", "ok");
        return load();
      })
      .catch(function (err) {
        alertBox.className = "alert alert-error";
        alertBox.textContent = err.message;
        alertBox.hidden = false;
      })
      .then(function () { $("btnSaveSettings").disabled = false; });
  }

  /* ---------------- 对话框基础 ---------------- */

  function showModal(id) { $(id).hidden = false; }
  function hideModal(id) { $(id).hidden = true; }

  var confirmResolve = null;

  function confirmDialog(title, text) {
    $("confirmTitle").textContent = title;
    $("confirmText").textContent = text;
    showModal("confirmModal");
    return new Promise(function (resolve) {
      confirmResolve = resolve;
    });
  }

  function resolveConfirm(value) {
    hideModal("confirmModal");
    if (confirmResolve) {
      confirmResolve(value);
      confirmResolve = null;
    }
  }

  /* ---------------- 事件绑定 ---------------- */

  function bind() {
    $("btnRefresh").addEventListener("click", function () {
      var btn = $("btnRefresh");
      btn.disabled = true;
      load().then(function () { btn.disabled = false; toast("状态已刷新", "ok"); })
        .catch(function () { btn.disabled = false; });
    });

    $("btnSettings").addEventListener("click", openSettings);
    $("btnUpgradeAll").addEventListener("click", upgradeAll);
    $("btnSaveSettings").addEventListener("click", saveSettings);
    $("btnReloadLogs").addEventListener("click", fetchLogs);
    $("logsContainer").addEventListener("change", function (e) {
      state.logsContainer = e.target.value;
      fetchLogs();
    });
    $("btnCopyYml").addEventListener("click", function () {
      var text = $("ymlContent").textContent;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { toast("内容已复制", "ok"); });
      }
    });
    $("confirmOk").addEventListener("click", function () { resolveConfirm(true); });

    $("searchInput").addEventListener("input", function (e) {
      state.q = e.target.value.trim();
      renderList();
    });

    $("filterGroup").addEventListener("click", function (e) {
      var btn = e.target.closest(".seg");
      if (!btn) return;
      state.filter = btn.getAttribute("data-filter");
      Array.prototype.forEach.call($("filterGroup").children, function (el) {
        el.classList.toggle("active", el === btn);
      });
      renderList();
    });

    $("detectBox").addEventListener("click", function (e) {
      var item = e.target.closest("[data-path]");
      if (item) $("inputBasePath").value = item.getAttribute("data-path");
    });

    document.addEventListener("click", function (e) {
      var closer = e.target.closest("[data-close]");
      if (closer) {
        var id = closer.getAttribute("data-close");
        if (id === "confirmModal") resolveConfirm(false);
        else hideModal(id);
      }
    });

    // 点击遮罩关闭
    ["settingsModal", "logsModal", "ymlModal", "confirmModal"].forEach(function (id) {
      $(id).addEventListener("click", function (e) {
        if (e.target === $(id)) {
          if (id === "confirmModal") resolveConfirm(false);
          else hideModal(id);
        }
      });
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        if (!$("confirmModal").hidden) return resolveConfirm(false);
        if (!$("settingsModal").hidden) return hideModal("settingsModal");
        if (!$("logsModal").hidden) return hideModal("logsModal");
        if (!$("ymlModal").hidden) return hideModal("ymlModal");
        if (!$("taskDrawer").hidden) $("taskDrawer").hidden = true;
      }
    });

    $("btnTaskClose").addEventListener("click", function () {
      $("taskDrawer").hidden = true;
      state.taskId = null;
      if (state.taskTimer) clearTimeout(state.taskTimer);
    });

    $("btnTaskCopy").addEventListener("click", function () {
      var text = $("terminal").textContent;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { toast("日志已复制", "ok"); });
      }
    });
  }

  /* ---------------- 启动 ---------------- */

  bind();
  load();

  // 每 25 秒静默刷新一次状态
  state.autoTimer = setInterval(function () {
    if (document.hidden) return;
    if (state.taskId) return;
    load(true);
  }, 25000);
})();

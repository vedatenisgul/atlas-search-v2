/*
 * app.js — Atlas Search reactive frontend (Alpine.js data components).
 *
 * PRD refs: §2.3 UI / Monitoring, §8 API Summary.
 *
 * Phase 5.2 pivot: this file holds only fetch logic + Alpine component state.
 * All rendering lives in the Jinja templates via x-text / x-show / x-for,
 * so the DOM is reconciled by Alpine on every 2s poll (no manual innerHTML,
 * no flicker).
 *
 * Registered on alpine:init:
 *   - $store.fmt          — shared formatters used from templates
 *   - layoutRoot()        — body shell; highlights the active nav link
 *   - crawlerForm()       — POST /api/crawler/create + recent-jobs cache
 *   - statusDashboard()   — polls /api/metrics, /api/crawler/list,
 *                           /api/crawler/history, /api/crawler/status/{id};
 *                           wires Pause / Resume / Stop / Delete; ticks
 *                           uptime locally every 1s between polls.
 *   - searchBrowser()     — GET /api/search with prev/next pagination.
 *
 * Owner agent: UI Agent.
 */

(function () {
  "use strict";

  // ================================================================= config
  //
  // ``window.ATLAS_CONFIG`` is a server-rendered snapshot of the knobs in
  // ``core/config.py`` (see ``api/routes.py::_atlas_config_for_templates``
  // and the <script> tag at the top of ``templates/base.html``). Reading
  // through this object keeps the frontend aligned with the backend
  // defaults without requiring a second build step or hardcoded copies.
  //
  // Fallbacks below are defensive — if a page happens to render without
  // the config (e.g. a unit test mounting the JS in isolation), every
  // component still boots with sane defaults that match the PRD.
  var ATLAS_CFG = (window && window.ATLAS_CONFIG) || {};
  function cfg(key, fallback) {
    var v = ATLAS_CFG[key];
    return (v === null || v === undefined) ? fallback : v;
  }

  // =================================================================== utils

  var STORAGE = {
    SELECTED:    "atlas:selected_job",
    RECENT_JOBS: "atlas:recent_jobs",
    LAST_QUERY:  "atlas:last_query",
  };

  function lsGet(key, fallback) {
    try {
      var raw = window.localStorage.getItem(key);
      if (raw === null || raw === undefined) return fallback;
      return JSON.parse(raw);
    } catch (e) { return fallback; }
  }

  function lsSet(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); }
    catch (e) { /* quota / privacy — ignore */ }
  }

  function fetchJSON(url, options) {
    var opts = options || {};
    opts.headers = Object.assign(
      { "Accept": "application/json" },
      opts.headers || {}
    );
    return fetch(url, opts).then(function (response) {
      var ctype = response.headers.get("content-type") || "";
      var body = ctype.indexOf("application/json") >= 0
        ? response.json().catch(function () { return null; })
        : response.text().catch(function () { return ""; });
      return body.then(function (data) {
        if (!response.ok) {
          var err = new Error("HTTP " + response.status);
          err.status = response.status;
          err.body = data;
          throw err;
        }
        return data;
      });
    });
  }

  function extractError(err) {
    if (!err) return "Request failed";
    if (err.body && typeof err.body === "object") {
      return err.body.detail || err.body.error || err.body.message || err.message;
    }
    if (typeof err.body === "string" && err.body) return err.body;
    return err.message || "Request failed";
  }

  // ============================================================== alpine init
  //
  // Registration must happen BEFORE Alpine's CDN script runs, because the
  // CDN build schedules `Alpine.start()` via `queueMicrotask`, so by the
  // time a later defer script runs the `alpine:init` event has already
  // fired. We defend against ordering mistakes two ways:
  //   1. The listener below is the normal path when this script loads
  //      before the Alpine CDN (see templates/base.html).
  //   2. If `window.Alpine` already exists at parse time, we call the
  //      same registration function immediately — Alpine's `data()` and
  //      `store()` APIs are safe to invoke both before and after start().
  //
  // This makes "Can't find variable: crawlerForm" impossible regardless
  // of how the <script> tags end up ordered.

  function atlasRegisterAlpine() {

    window.Alpine.store("fmt", {
      int: function (n) {
        if (n === null || n === undefined || isNaN(n)) return "—";
        return Math.floor(Number(n)).toLocaleString();
      },
      float: function (n, digits) {
        if (n === null || n === undefined || isNaN(n)) return "—";
        return Number(n).toFixed(digits == null ? 2 : digits);
      },
      duration: function (seconds) {
        if (seconds === null || seconds === undefined || isNaN(seconds)) return "—";
        var s = Math.max(0, Math.floor(Number(seconds)));
        var h = Math.floor(s / 3600);
        var m = Math.floor((s % 3600) / 60);
        var ss = s % 60;
        var pad = function (v) { return v < 10 ? "0" + v : String(v); };
        return (h > 0 ? h + ":" + pad(m) : pad(m)) + ":" + pad(ss);
      },
      timestamp: function (ts) {
        if (!ts) return "";
        var ms = Number(ts);
        if (!isFinite(ms)) return "";
        if (ms < 1e12) ms = ms * 1000;
        var d = new Date(ms);
        var pad = function (v) { return v < 10 ? "0" + v : String(v); };
        return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
      },
      datetime: function (ts) {
        if (!ts) return "";
        var ms = Number(ts);
        if (!isFinite(ms)) return "";
        if (ms < 1e12) ms = ms * 1000;
        return new Date(ms).toLocaleString();
      },
      // --- status pill helpers ---
      statusTone: function (label) {
        var s = String(label || "").toLowerCase();
        if (s.indexOf("critical") >= 0)                          return "rose";
        if (s === "running"   || s === "healthy")                return "emerald";
        if (s === "paused"    || s.indexOf("back") >= 0)         return "amber";
        if (s === "stopped"   || s === "error" || s === "failed") return "rose";
        // "deleted" is a terminal state the user initiated — show it as a
        // muted slate pill so it's visually distinct from "stopped" (which
        // implies a crash / abort) and from "completed" (the happy path).
        if (s === "deleted")                                     return "slate";
        if (s === "completed")                                   return "sky";
        return "slate";
      },
      pillClass: function (label) {
        var tone = this.statusTone(label);
        var base = "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium";
        var tones = {
          emerald: " border-emerald-400/30 bg-emerald-500/10 text-emerald-300 shadow-glow-emerald",
          amber:   " border-amber-400/30   bg-amber-500/10   text-amber-200   shadow-glow-amber",
          rose:    " border-rose-400/30    bg-rose-500/10    text-rose-300    shadow-glow-rose",
          sky:     " border-sky-400/30     bg-sky-500/10     text-sky-300     shadow-glow-sky",
          slate:   " border-white/10       bg-white/5        text-slate-300",
        };
        return base + (tones[tone] || tones.slate);
      },
      dotClass: function (label) {
        var tone = this.statusTone(label);
        var m = { emerald: "live-dot", amber: "live-dot live-dot--amber",
                  rose: "live-dot live-dot--rose", sky: "live-dot",
                  slate: "live-dot live-dot--slate" };
        return m[tone] || m.slate;
      },
      // --- log level tone ---
      logLevelClass: function (level) {
        var s = String(level || "info").toLowerCase();
        if (s === "error") return "text-rose-300";
        if (s === "warn" || s === "warning") return "text-amber-300";
        return "text-slate-400";
      },
    });

    // ---------------------------------------------------------- layoutRoot

    window.Alpine.data("layoutRoot", function () {
      return {
        init: function () {
          var page = (document.body.dataset.page || "").trim();
          document.querySelectorAll("[data-nav]").forEach(function (link) {
            link.classList.toggle("is-active", link.getAttribute("data-nav") === page);
          });
        }
      };
    });

    // ---------------------------------------------------------- crawlerForm

    window.Alpine.data("crawlerForm", function () {
      // Defaults sourced from the server-rendered ATLAS_CONFIG so the UI
      // stays aligned with ``core/config.py`` (DEFAULT_MAX_DEPTH et al.)
      // instead of shipping its own hardcoded copy.
      var FORM_DEFAULTS = {
        seed_url: "",
        max_depth:    cfg("DEFAULT_MAX_DEPTH", 3),
        hit_rate:     cfg("DEFAULT_HIT_RATE", 2),
        max_capacity: cfg("DEFAULT_MAX_CAPACITY", 10000),
        max_urls:     cfg("DEFAULT_MAX_URLS", 1000),
      };
      return {
        form: Object.assign({}, FORM_DEFAULTS),
        submitting: false,
        resetting: false,
        status: { tone: "", message: "" },
        recent: [],
        // Live stats keyed by job_id — { crawled, status, max_urls, pending }.
        // Populated from /api/crawler/list + /api/crawler/history so the
        // "Recently created" list can show "crawled N URLs" and the current
        // job state (running / completed / stopped / deleted) even after a
        // page reload that otherwise only carries the form payload.
        jobStats: {},
        // Flips to true after the first successful /api/crawler/list +
        // history round-trip so ``recentStatusLabel`` can distinguish
        // "not yet polled" (shows "pending") from "polled and the job is
        // gone from both active and history" (shows "deleted").
        _statsReady: false,
        _statsTimer: null,

        init: function () {
          this.recent = lsGet(STORAGE.RECENT_JOBS, []) || [];
          this.refreshStats();
          var self = this;
          // Poll every 3s so the counter ticks up while a crawl is active
          // without hammering the API the way the Status dashboard does.
          this._statsTimer = window.setInterval(function () {
            self.refreshStats();
          }, 3000);
        },

        destroy: function () {
          if (this._statsTimer) window.clearInterval(this._statsTimer);
        },

        refreshStats: function () {
          var self = this;
          Promise.all([
            fetchJSON("/api/crawler/list").catch(function () { return null; }),
            fetchJSON("/api/crawler/history").catch(function () { return null; }),
          ]).then(function (values) {
            var stats = Object.create(null);
            var ingest = function (list, fallbackStatus) {
              if (!list) return;
              var arr = Array.isArray(list) ? list
                      : Array.isArray(list.jobs) ? list.jobs
                      : Array.isArray(list.history) ? list.history
                      : [];
              for (var i = 0; i < arr.length; i++) {
                var j = arr[i];
                if (!j || !j.job_id) continue;
                // History may duplicate a live record for a deleted job —
                // whichever comes first (active-first order) wins and later
                // entries are skipped so the live state is authoritative.
                if (stats[j.job_id]) continue;
                stats[j.job_id] = {
                  crawled: j.crawled != null ? j.crawled : 0,
                  status:  j.status  != null ? j.status  : fallbackStatus,
                  pending: (j.queue || {}).pending || 0,
                  capacity: (j.queue || {}).capacity || 0,
                  // max_urls is the user-requested URL budget — surfaced so
                  // "Recently created" can render "N / max_urls" instead of
                  // falling back to queue capacity.
                  max_urls: j.max_urls != null ? j.max_urls : null,
                  deleted: !!j.deleted || String(j.status || "").toLowerCase() === "deleted",
                };
              }
            };
            ingest(values[0], "running");
            ingest(values[1], "completed");
            self.jobStats = stats;
            // Mark ready only after a round-trip where at least one of
            // the two sources returned (null from both = network offline,
            // don't claim "deleted" just because we couldn't reach the
            // API). ``values`` is [list, history]; either non-null counts.
            if (values[0] !== null || values[1] !== null) {
              self._statsReady = true;
            }
          });
        },

        statsFor: function (jobId) {
          return (jobId && this.jobStats[jobId]) || null;
        },

        recentStatusLabel: function (entry) {
          var s = this.statsFor(entry && entry.job_id);
          if (s && s.status) return s.status;
          // The job_id is no longer reported by /api/crawler/list or
          // /api/crawler/history — it was deleted and cascade-purged. Show
          // that explicitly so users aren't stuck staring at a "pending"
          // pill for a crawl that will never resume.
          if (entry && entry.job_id && this.jobStats && this._statsReady) return "deleted";
          return "pending";
        },

        recentCrawledLabel: function (entry) {
          var s = this.statsFor(entry && entry.job_id);
          if (!s) return "—";
          // Progress denominator is the requested max_urls — never the
          // queue capacity. Order of preference:
          //   1. live API payload (most authoritative),
          //   2. the form payload snapshotted in localStorage,
          //   3. no denominator (bare count) if neither is known.
          var target = (s.max_urls != null) ? s.max_urls
                     : (entry && entry.max_urls != null) ? entry.max_urls
                     : null;
          var n = this.$store.fmt.int(s.crawled);
          return (target != null) ? (n + " / " + this.$store.fmt.int(target)) : n;
        },

        validationError: function () {
          var f = this.form;
          if (!f.seed_url) return "Seed URL is required.";
          if (!/^https?:\/\//i.test(f.seed_url)) return "Seed URL must start with http:// or https://.";
          if (!isFinite(+f.max_depth)    || +f.max_depth    < 0)  return "Max depth must be zero or more.";
          if (!isFinite(+f.hit_rate)     || +f.hit_rate     <= 0) return "Hit rate must be greater than zero.";
          if (!isFinite(+f.max_capacity) || +f.max_capacity < 1)  return "Queue capacity must be at least 1.";
          if (!isFinite(+f.max_urls)     || +f.max_urls     < 1)  return "Max URLs must be at least 1.";
          return null;
        },

        submit: function () {
          var self = this;
          self.status = { tone: "", message: "" };

          var validation = self.validationError();
          if (validation) {
            self.status = { tone: "rose", message: validation };
            return;
          }

          var payload = {
            seed_url: String(self.form.seed_url).trim(),
            max_depth: Number(self.form.max_depth),
            hit_rate: Number(self.form.hit_rate),
            max_capacity: Number(self.form.max_capacity),
            max_urls: Number(self.form.max_urls),
          };

          self.submitting = true;
          self.status = { tone: "sky", message: "Submitting job…" };

          fetchJSON("/api/crawler/create", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          })
            .then(function (data) {
              var jobId = (data && (data.job_id || data.id)) || "";
              self.status = {
                tone: "emerald",
                message: jobId
                  ? "Job created — " + jobId + ". Opening status…"
                  : "Job created. Opening status…",
              };
              var entry = {
                job_id: jobId,
                seed_url: payload.seed_url,
                max_depth: payload.max_depth,
                hit_rate: payload.hit_rate,
                max_capacity: payload.max_capacity,
                max_urls: payload.max_urls,
                created_at: Date.now() / 1000,
              };
              self.recent = self._remember(entry);
              if (jobId) lsSet(STORAGE.SELECTED, jobId);
              // Fire an immediate stats refresh so the "Recently created"
              // list reflects the brand-new job before we navigate away.
              self.refreshStats();
              window.setTimeout(function () {
                window.location.href = "/status";
              }, 700);
            })
            .catch(function (err) {
              self.status = { tone: "rose", message: "Failed: " + extractError(err) };
            })
            .then(function () { self.submitting = false; });
        },

        reset: function () {
          this.form = Object.assign({}, FORM_DEFAULTS);
          this.status = { tone: "", message: "" };
        },

        // Nuclear option: wipe every crawl, the AtlasTrie index, the job
        // history, and all persisted local caches. Fires POST /api/system/reset
        // which the backend guards with the NoSQLStore.lock + AtlasTrie.RLock,
        // so the call is safe to issue even while crawlers are indexing.
        resetAll: function () {
          var self = this;
          if (self.resetting) return;
          if (!window.confirm(
            "Reset ALL Atlas Search data?\n\n" +
            "This stops every running crawl, clears the search index, and " +
            "purges job history. This action cannot be undone."
          )) return;

          // Clear the UI synchronously so the Recently-created list and the
          // selected-job pointer disappear the instant the user confirms.
          // The /api/system/reset call below can take a few hundred ms to
          // a few seconds (shutdown_all_workers waits on each live thread)
          // and we don't want the user staring at stale cards in the meantime.
          try {
            window.localStorage.removeItem(STORAGE.SELECTED);
            window.localStorage.removeItem(STORAGE.RECENT_JOBS);
            window.localStorage.removeItem(STORAGE.LAST_QUERY);
          } catch (e) { /* quota/privacy — ignore */ }
          self.recent = [];
          self.jobStats = {};
          self.resetting = true;
          self.status = { tone: "sky", message: "Resetting all data…" };

          fetchJSON("/api/system/reset", { method: "POST" })
            .then(function () {
              self.status = {
                tone: "emerald",
                message: "All data has been reset.",
              };
              // One more refresh so any still-running worker that died mid-
              // reset flips to "deleted" / disappears on the next poll.
              self.refreshStats();
            })
            .catch(function (err) {
              self.status = {
                tone: "rose",
                message: "Reset failed: " + extractError(err),
              };
            })
            .then(function () { self.resetting = false; });
        },

        openRecent: function (entry) {
          if (entry && entry.job_id) lsSet(STORAGE.SELECTED, entry.job_id);
          window.location.href = "/status";
        },

        _remember: function (entry) {
          var list = this.recent.slice();
          list.unshift(entry);
          var out = [];
          var seen = Object.create(null);
          for (var i = 0; i < list.length; i++) {
            var key = list[i].job_id || (list[i].seed_url + ":" + list[i].created_at);
            if (seen[key]) continue;
            seen[key] = true;
            out.push(list[i]);
            if (out.length >= 5) break;
          }
          lsSet(STORAGE.RECENT_JOBS, out);
          return out;
        },

        statusClass: function () {
          var t = this.status.tone;
          if (t === "rose")    return "text-rose-300";
          if (t === "emerald") return "text-emerald-300";
          if (t === "sky")     return "text-sky-300";
          return "text-slate-400";
        },
      };
    });

    // ------------------------------------------------------- statusDashboard

    // Sourced from ATLAS_CONFIG — ``core/config.POLL_INTERVAL_MS`` and
    // ``UI_TICK_INTERVAL_MS``. Cached into module-level vars here so the
    // setInterval closures don't re-read the global every tick.
    var POLL_INTERVAL_MS = cfg("POLL_INTERVAL_MS", 2000);
    var TICK_INTERVAL_MS = cfg("UI_TICK_INTERVAL_MS", 1000);

    window.Alpine.data("statusDashboard", function () {
      return {
        selectedId: null,
        jobs: [],
        snapshot: null,
        metrics: null,
        lastPollOk: null,
        lastPollAt: 0,
        lastKnownUptime: null,
        uptimeDisplay: "—",
        inFlight: false,
        everLoaded: false,
        _pollTimer: null,
        _tickTimer: null,

        init: function () {
          var self = this;
          self.selectedId = lsGet(STORAGE.SELECTED, null);

          self.poll();
          self._pollTimer = window.setInterval(function () { self.poll(); }, POLL_INTERVAL_MS);
          self._tickTimer = window.setInterval(function () { self._tickUptime(); }, TICK_INTERVAL_MS);

          document.addEventListener("visibilitychange", function () {
            if (!document.hidden) self.poll();
          });
        },

        destroy: function () {
          if (this._pollTimer) window.clearInterval(this._pollTimer);
          if (this._tickTimer) window.clearInterval(this._tickTimer);
        },

        // ------------------------------------------------------------ derived

        get logs() {
          return (this.snapshot && Array.isArray(this.snapshot.logs))
            ? this.snapshot.logs
            : [];
        },

        get queue() {
          return (this.snapshot && this.snapshot.queue) || {};
        },

        get bpLabel() {
          return (this.queue && this.queue.status) || "—";
        },

        get statusLabel() {
          if (!this.snapshot) return "idle";
          if (this.snapshot.status) return this.snapshot.status;
          return this.snapshot.paused ? "paused" : "idle";
        },

        get isRunning() {
          return this.snapshot
            && !this.snapshot.paused
            && !this.snapshot.ended_at
            && !this.snapshot.stopping;
        },

        get isPaused() { return !!(this.snapshot && this.snapshot.paused); },

        get isTerminated() {
          return !!(this.snapshot && (this.snapshot.ended_at
                 || String(this.snapshot.status || "").toLowerCase() === "stopped"));
        },

        get speedDisplay() {
          return this.snapshot ? this.$store.fmt.float(this.snapshot.effective_speed, 2) : "—";
        },

        get crawledDisplay() {
          return this.snapshot ? this.$store.fmt.int(this.snapshot.crawled) : "—";
        },

        // Target URL budget requested when the job was created. Rendered
        // as the denominator of "Total crawled" so users see progress
        // toward the cap, not just the running count.
        get maxUrlsDisplay() {
          if (!this.snapshot) return "—";
          var m = this.snapshot.max_urls;
          return m != null ? this.$store.fmt.int(m) : "—";
        },

        get crawledProgressDisplay() {
          return this.crawledDisplay + " / " + this.maxUrlsDisplay;
        },

        get errorsDisplay() {
          return this.snapshot ? this.$store.fmt.int(this.snapshot.errors || 0) : "0";
        },

        get pendingDisplay() { return this.$store.fmt.int(this.queue.pending); },
        get capacityDisplay() { return this.$store.fmt.int(this.queue.capacity); },

        get droppedDisplay() {
          var q = this.queue;
          var d = q.dropped_total != null ? q.dropped_total : q.dropped;
          return this.$store.fmt.int(d || 0);
        },

        get startedAtDisplay() {
          if (!this.snapshot || !this.snapshot.started_at) return "not started";
          return "started " + this.$store.fmt.datetime(this.snapshot.started_at);
        },

        get pollIndicatorText() {
          if (this.lastPollOk === null) return "Connecting…";
          if (this.lastPollOk) return "Live · " + this.$store.fmt.timestamp(this.lastPollAt);
          return "Offline";
        },

        get pollIndicatorClass() {
          if (this.lastPollOk === null) return this.$store.fmt.pillClass("idle");
          return this.$store.fmt.pillClass(this.lastPollOk ? "healthy" : "stopped");
        },

        get pollIndicatorDot() {
          if (this.lastPollOk === null) return this.$store.fmt.dotClass("idle");
          return this.$store.fmt.dotClass(this.lastPollOk ? "running" : "stopped");
        },

        // -------------------------------------------------------------- poll

        poll: function () {
          var self = this;
          if (self.inFlight) return;
          self.inFlight = true;

          var jobsP    = fetchJSON("/api/crawler/list").catch(function () { return null; });
          var metricsP = fetchJSON("/api/metrics").catch(function () { return null; });
          var historyP = fetchJSON("/api/crawler/history").catch(function () { return null; });

          Promise.all([jobsP, metricsP, historyP])
            .then(function (values) {
              var active  = self._normalizeJobs(values[0]);
              var history = self._normalizeJobs(values[2]);
              self.metrics = values[1] || null;
              self.jobs = self._mergeJobs(active, history);
              self.selectedId = self._resolveSelectedId();

              if (self.selectedId) {
                return fetchJSON("/api/crawler/status/" + encodeURIComponent(self.selectedId))
                  .catch(function () { return null; })
                  .then(function (snap) { self.snapshot = snap || null; });
              }
              self.snapshot = null;
              return null;
            })
            .then(function () {
              self.lastPollOk = true;
              self.lastPollAt = Date.now();
              self.everLoaded = true;
              if (self.snapshot && isFinite(self.snapshot.uptime_seconds)) {
                self.lastKnownUptime = Number(self.snapshot.uptime_seconds);
              }
              self._tickUptime();
            })
            .catch(function () { self.lastPollOk = false; })
            .then(function () { self.inFlight = false; });
        },

        _tickUptime: function () {
          if (!this.snapshot) { this.uptimeDisplay = "—"; return; }
          var base = this.lastKnownUptime;
          if (base == null) { this.uptimeDisplay = "—"; return; }
          if (this.snapshot.ended_at || this.snapshot.paused || !this.lastPollOk) {
            this.uptimeDisplay = this.$store.fmt.duration(base);
            return;
          }
          var elapsed = Math.max(0, (Date.now() - this.lastPollAt) / 1000);
          this.uptimeDisplay = this.$store.fmt.duration(base + elapsed);
        },

        // ----------------------------------------------------------- actions

        runAction: function (action) {
          var self = this;
          var id = self.selectedId;
          if (!id) return;

          var url, method;
          if (action === "pause")  { url = "/api/crawler/pause/"  + encodeURIComponent(id); method = "POST"; }
          else if (action === "resume") { url = "/api/crawler/resume/" + encodeURIComponent(id); method = "POST"; }
          else if (action === "stop")   { url = "/api/crawler/stop/"   + encodeURIComponent(id); method = "POST"; }
          else if (action === "delete") {
            if (!window.confirm("Delete job " + id + "? This archives and purges it.")) return;
            url = "/api/crawler/delete/" + encodeURIComponent(id); method = "DELETE";
          } else { return; }

          fetchJSON(url, { method: method })
            .catch(function () { /* surfaced via next poll */ })
            .then(function () {
              if (action === "delete" && self.selectedId === id) {
                self.selectedId = null;
                lsSet(STORAGE.SELECTED, null);
                self.snapshot = null;
                self.lastKnownUptime = null;
                self.uptimeDisplay = "—";
              }
              self.poll();
            });
        },

        selectJob: function (id) {
          if (!id || id === this.selectedId) return;
          this.selectedId = id;
          lsSet(STORAGE.SELECTED, id);
          this.snapshot = null;
          this.lastKnownUptime = null;
          this.uptimeDisplay = "—";
          this.poll();
        },

        // --------------------------------------------------------- internals

        _normalizeJobs: function (raw) {
          if (!raw) return [];
          if (Array.isArray(raw)) return raw;
          if (Array.isArray(raw.jobs))    return raw.jobs;
          if (Array.isArray(raw.active))  return raw.active;
          if (Array.isArray(raw.history)) return raw.history;
          if (Array.isArray(raw.items))   return raw.items;
          if (typeof raw === "object") {
            return Object.keys(raw).map(function (k) {
              var v = raw[k];
              if (v && typeof v === "object" && !v.job_id) v.job_id = k;
              return v;
            });
          }
          return [];
        },

        _mergeJobs: function (active, history) {
          var out = [];
          var seen = Object.create(null);
          function push(list, flag) {
            for (var i = 0; i < list.length; i++) {
              var j = list[i];
              if (!j || !j.job_id) continue;
              if (seen[j.job_id]) continue;
              seen[j.job_id] = true;
              j.__active = flag;
              out.push(j);
            }
          }
          push(active || [], true);
          push(history || [], false);
          out.sort(function (a, b) {
            if (a.__active !== b.__active) return a.__active ? -1 : 1;
            return (b.started_at || 0) - (a.started_at || 0);
          });
          return out;
        },

        _resolveSelectedId: function () {
          if (this.selectedId) {
            for (var i = 0; i < this.jobs.length; i++) {
              if (this.jobs[i].job_id === this.selectedId) return this.selectedId;
            }
          }
          var firstActive = this.jobs.find(function (j) { return j.__active; });
          if (firstActive) { lsSet(STORAGE.SELECTED, firstActive.job_id); return firstActive.job_id; }
          if (this.jobs.length) { lsSet(STORAGE.SELECTED, this.jobs[0].job_id); return this.jobs[0].job_id; }
          return null;
        },
      };
    });

    // -------------------------------------------------------- searchBrowser

    window.Alpine.data("searchBrowser", function () {
      return {
        query: "",
        limit: 10,
        offset: 0,
        results: [],
        total: 0,
        loading: false,
        error: "",
        elapsedMs: 0,
        lastQuery: "",

        init: function () {
          var last = lsGet(STORAGE.LAST_QUERY, "");
          if (last) {
            this.query = last;
            this.submit();
          }
        },

        get hasResults() { return this.results.length > 0; },
        get showEmptyState() {
          return !this.loading && !this.error && this.lastQuery && this.results.length === 0;
        },
        get showResults() { return !this.error && this.results.length > 0; },
        get paginationVisible() {
          return !this.error && (this.results.length > 0 || this.offset > 0);
        },
        get prevDisabled() { return this.offset === 0 || this.loading; },
        get nextDisabled() {
          // Prefer the authoritative ``total`` when the API returned it — the
          // previous check (page size < limit) mis-flagged "next" as enabled
          // when the current page happened to be exactly full.
          if (this.loading) return true;
          if (this.total > 0) return this.offset + this.results.length >= this.total;
          return this.results.length < this.limit;
        },
        get pageNumber() { return Math.floor(this.offset / this.limit) + 1; },
        get totalPages() {
          if (!this.total || !this.limit) return 1;
          return Math.max(1, Math.ceil(this.total / this.limit));
        },
        get totalText() {
          if (this.total <= 0) return "";
          return this.total === 1 ? "1 result" : (this.total.toLocaleString() + " results");
        },
        get rangeText() {
          if (this.results.length === 0) return "";
          var from = this.offset + 1;
          var to = this.offset + this.results.length;
          if (this.total > 0) {
            return "Results " + from + "–" + to + " of " + this.total.toLocaleString();
          }
          return "Results " + from + "–" + to;
        },

        submit: function () {
          this.offset = 0;
          this._run();
        },

        next: function () {
          if (this.nextDisabled) return;
          this.offset += this.limit;
          this._run();
        },

        prev: function () {
          if (this.prevDisabled) return;
          this.offset = Math.max(0, this.offset - this.limit);
          this._run();
        },

        _run: function () {
          var self = this;
          var q = String(self.query || "").trim();
          if (!q) {
            self.results = [];
            self.error = "";
            self.lastQuery = "";
            return;
          }
          lsSet(STORAGE.LAST_QUERY, q);
          self.loading = true;
          self.error = "";
          self.lastQuery = q;

          var url = "/api/search"
            + "?q="      + encodeURIComponent(q)
            + "&limit="  + encodeURIComponent(self.limit)
            + "&offset=" + encodeURIComponent(self.offset);

          var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
          fetchJSON(url)
            .then(function (body) {
              var list = Array.isArray(body) ? body
                       : (body && Array.isArray(body.results)) ? body.results
                       : [];
              // ``total`` is the full cross-page match count — the UI renders
              // it as "N results". Fall back to the current page size for
              // older responses that didn't carry the field.
              self.total = (body && typeof body.total === "number")
                ? body.total
                : list.length;
              self.results = list.map(function (r) {
                return {
                  url:             r.url || r.relevant_url || "",
                  origin_url:      r.origin_url || r.origin || "",
                  title:           r.title || r.url || r.relevant_url || "(untitled)",
                  snippet:         r.snippet || "",
                  depth:           r.depth != null ? r.depth : null,
                  frequency:       r.frequency != null ? r.frequency
                                  : (r.term_frequency != null ? r.term_frequency : null),
                  relevance_score: r.relevance_score != null ? r.relevance_score
                                  : (r.score != null ? r.score : null),
                };
              });
              // Prefer the server-reported elapsed time (wallclock inside
              // the search pipeline, excluding network latency). Fall back
              // to the client-side delta for responses that don't carry
              // ``elapsed_ms`` — older builds or partial mocks.
              var t1 = (window.performance && performance.now) ? performance.now() : Date.now();
              var serverMs = body && typeof body.elapsed_ms === "number" ? body.elapsed_ms : null;
              self.elapsedMs = (serverMs !== null && serverMs >= 0)
                ? serverMs
                : Math.max(0, Math.round(t1 - t0));
            })
            .catch(function (err) {
              self.results = [];
              self.total = 0;
              self.error = extractError(err);
            })
            .then(function () { self.loading = false; });
        },
      };
    });
  }

  // Belt-and-suspenders: register immediately if Alpine is already loaded
  // (e.g. script tags got reordered), otherwise wait for the normal event.
  if (window.Alpine) {
    atlasRegisterAlpine();
  } else {
    document.addEventListener("alpine:init", atlasRegisterAlpine);
  }
})();

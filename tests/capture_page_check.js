// Runs the script of capture/page.html against a small fake DOM and a fake server,
// to check the logic of the page (numbering, the upload queue, retakes, the opened page).
// Usage: node capture_page_check.js path/to/page.html   (run by tests/test_capture.py)
"use strict";
const fs = require("fs");

// ---------------------------------------------------------------- a fake DOM
function element(tag) {
  const e = {
    tag, children: [], handlers: {}, attributes: {}, hidden: false, disabled: false, value: "", className: "",
    clicks: 0, _text: "",
    classList: {
      add(c) { if (!this.contains(c)) e.className = (e.className + " " + c).trim(); },
      remove(c) { e.className = e.className.split(" ").filter(x => x && x !== c).join(" "); },
      contains(c) { return e.className.split(" ").includes(c); },
      toggle(c, on) { (on === undefined ? !this.contains(c) : on) ? this.add(c) : this.remove(c); },
    },
    setAttribute(k, v) { e.attributes[k] = String(v); },
    getAttribute(k) { return e.attributes[k]; },
    addEventListener(type, handler) { e.handlers[type] = handler; },
    appendChild(c) { e.children.push(c); return c; },
    insertBefore(c, before) {
      const at = e.children.indexOf(c);
      if (at >= 0) e.children.splice(at, 1);
      const to = before ? e.children.indexOf(before) : -1;
      if (to >= 0) e.children.splice(to, 0, c); else e.children.push(c);
    },
    replaceChild(fresh, old) { e.children[e.children.indexOf(old)] = fresh; },
    removeChild(c) { e.children.splice(e.children.indexOf(c), 1); },
    focus() { e.focused = true; },
    click() { e.clicks += 1; if (e.handlers.click) e.handlers.click(); },
  };
  Object.defineProperty(e, "textContent", {
    get() { return e._text + e.children.map(c => c.textContent).join(""); },
    set(v) { e._text = String(v); e.children = []; },
  });
  return e;
}
const byId = {};
const body = element("body");
global.document = {
  title: "", body,
  getElementById: id => byId[id] || (byId[id] = element(id)),
  createElement: tag => element(tag),
  createTextNode: text => ({ textContent: text }),
};
const windowHandlers = {};
const backs = [];
const timers = [];  // the page's own polling; a check runs it when it wants a poll
global.window = {
  setTimeout: callback => { timers.push(callback); return 0; }, scrollTo: () => {},
  addEventListener: (type, handler) => { windowHandlers[type] = handler; },
};
let backCalls = 0;
global.history = {
  pushState: state => backs.push(state),
  // Like a browser: the step back happens a moment later, not inside the call.
  back: () => { backCalls += 1; setImmediate(() => { backs.pop(); windowHandlers.popstate(); }); },
};

// ---------------------------------------------------------------- a fake basira
const server = {
  status: { name: "book", pages: [], next_page: 1, stopped: null, max_bytes: 1000 },
  texts: {}, calls: [], offline: false, loseAnswer: false, down: false, gone: false,
};
function answer(status, data) {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(data) });
}
global.fetch = (url, options) => {
  server.calls.push((options && options.method === "POST" ? "POST " : "GET ") + url);
  if (url === "status") {
    if (server.offline || server.down) return Promise.reject(new TypeError("Load failed"));
    if (server.gone) return answer(404, {});
    return answer(200, JSON.parse(JSON.stringify(server.status)));
  }
  if (url.startsWith("page/")) {
    const page = Number(url.slice(5));
    return answer(200, { page, state: "done", text: server.texts[page] || "", reason: "", version: 1, image: true });
  }
  if (server.down) return Promise.reject(new TypeError("Load failed"));
  const page = Number(/page=(\d+)/.exec(url)[1]);
  const replace = url.includes("replace=1");
  const there = server.status.pages.find(p => p.page === page);
  if (options.body.kind === "other") return answer(415, { error: "this file is not a JPEG or PNG photo" });
  if (there && !replace) return answer(409, { error: "page " + page + " is already there; use retake to replace it" });
  server.status.pages = server.status.pages.filter(p => p.page !== page).concat(
    { page, state: "queued", preview: "", reason: "", version: there ? there.version + 1 : 1, image: true });
  server.status.pages.sort((a, b) => a.page - b.page);
  if (server.loseAnswer) return Promise.reject(new TypeError("Load failed"));
  return answer(200, { page });
};

// ---------------------------------------------------------------- the page
const html = fs.readFileSync(process.argv[2], "utf8");
eval(/<script>([\s\S]*)<\/script>/.exec(html)[1]);

const $ = id => byId[id];
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };
let failures = 0;
function check(condition, what, detail) {
  if (condition) return;
  failures += 1;
  const shown = (key, value) => (["li", "open", "actions"].includes(key) ? undefined : value);
  console.error("FAIL: " + what + (detail === undefined ? "" : " -> " + JSON.stringify(detail, shown)));
}
function rows() {
  return $("pages").children.map(li => {
    const open = li.children[0], rowBody = open.children[1];
    const head = rowBody.children[0];
    return {
      li, open,
      page: Number(head.children[0].textContent.replace("Page ", "")),
      state: head.children[1].textContent,
      text: rowBody.children[1] ? rowBody.children[1].textContent : "",
      thumb: open.children[0].src || "",
      actions: li.children[1] ? li.children[1].children : [],
    };
  });
}
const row = page => rows().find(r => r.page === page);
const action = (page, label) => row(page).actions.find(b => b.textContent === label);
const photo = (size = 10, kind = "jpeg") => ({ size, kind });
async function shoot(file) {
  $("camera").files = [file || photo()];
  $("camera").handlers.change();
  await settle();
}
const posts = () => server.calls.filter(c => c.startsWith("POST "));
const setDone = (page, preview) => Object.assign(server.status.pages.find(p => p.page === page),
  { state: "done", preview });

(async () => {
  await settle();

  // 1. numbering from the session, the shutter opens the phone's camera
  check($("next").value === 1 && $("take").textContent === "Take a page", "field starts at the session's next page",
        [$("next").value, $("take").textContent]);
  $("take").click();
  check($("camera").clicks === 1, "the button opens the camera");
  $("camera").files = [photo()];
  $("camera").handlers.change();
  check($("next").value === 2, "the number moves on at once, before the upload ends", $("next").value);
  await settle();
  check(posts().join() === "POST upload?page=1", "page 1 uploaded once, as a new page", posts());
  check(row(1) && row(1).state === "Waiting", "page 1 is listed as waiting", rows().map(r => [r.page, r.state]));

  // 2. an upload whose status refresh fails stays in the list
  server.offline = true;
  $("take").click();
  await shoot();
  check(row(2) && row(2).state === "Waiting", "page 2 stays listed while the computer is unreachable",
        rows().map(r => [r.page, r.state]));
  check(!$("offline").hidden, "the lost connection is shown");
  $("next").value = "2";
  $("next").handlers.input();
  check($("take").textContent === "Retake page 2", "a page that only this phone knows about is known", $("take").textContent);
  server.offline = false;
  $("next").value = "3";
  $("next").handlers.input();

  // 3. a photo that is too large never leaves the phone; discarding it frees the number
  $("take").click();
  await shoot(photo(5000));
  check(row(3).state === "Not uploaded" && row(3).text.startsWith("The photo is too large"), "too large is said",
        row(3));
  check($("next").value === 4, "numbering went on to 4", $("next").value);
  check(row(3).actions.map(b => b.textContent).join() === "Send again,Take again,Discard", "a failed upload offers three ways",
        row(3).actions.map(b => b.textContent));
  action(3, "Discard").click();
  check(!row(3) && $("next").value === 3, "after discarding, the number is offered again", $("next").value);

  // 4. a refused photo shows the reason; "Take again" replaces it with a new photo
  $("take").click();
  await shoot(photo(10, "other"));
  check(row(3).state === "Not uploaded" && row(3).text === "This file is not a JPEG or PNG photo.",
        "refusal reason shown", row(3));
  const clicksBefore = $("camera").clicks;
  action(3, "Take again").click();
  check($("camera").clicks === clicksBefore + 1, "Take again opens the camera");
  await shoot();
  check(row(3).state === "Waiting" && posts().includes("POST upload?page=3"), "the new photo of page 3 is uploaded",
        [row(3).state, posts()]);
  check($("next").value === 4, "a retake does not move the number", $("next").value);

  // 5. the connection is down: the photo stays on the phone and is sent again later, once
  server.down = true;
  $("take").click();
  await shoot();
  check(row(4).state === "Not uploaded" && row(4).text.startsWith("The photo did not reach basira"),
        "an upload that got no answer is shown as not uploaded", row(4));
  server.down = false;
  action(4, "Send again").click();
  await settle();
  check(posts().filter(c => c === "POST upload?page=4").length === 2 && row(4).state === "Waiting",
        "Send again sends it once more", [posts(), row(4)]);

  // 5b. the photo arrived but its answer was lost: the page shows what the computer has,
  //     and nothing offers to send (and read) it a second time
  $("next").value = "40";
  $("next").handlers.input();
  server.loseAnswer = true;
  $("take").click();
  await shoot();
  server.loseAnswer = false;
  check(row(40).state === "Waiting" && row(40).actions.length === 0, "a lost answer is not a lost photo", row(40));
  check(posts().filter(c => c.startsWith("POST upload?page=40")).length === 1, "it was sent once", posts());

  // 5c. the computer has the page from somewhere else: nothing is replaced without saying so
  server.status.pages.push({ page: 41, state: "done", preview: "from another phone", reason: "", version: 1, image: true });
  $("take").click();  // the number moved on to 41; this phone does not know the page yet
  await shoot();
  check(row(41).state === "Not uploaded" && row(41).text.startsWith("Page 41 is already there"), "a taken number is refused", row(41));
  check(row(41).actions.map(b => b.textContent).join() === "Replace page 41,Discard", "replacing is offered by name",
        row(41).actions.map(b => b.textContent));
  check(!posts().includes("POST upload?page=41&replace=1"), "and not done silently", posts());
  action(41, "Discard").click();
  check(row(41).state === "Done" && row(41).text === "from another phone", "discarding leaves the other page", row(41));
  server.status.pages = server.status.pages.filter(p => p.page < 40);  // back to the small book
  timers.pop()();
  await settle();
  $("next").value = "5";
  $("next").handlers.input();

  // 6. typing a number that is there turns the shutter into a retake
  $("next").value = "1";
  $("next").handlers.input();
  check($("take").textContent === "Retake page 1" && $("take").classList.contains("retake"), "the button says Retake page 1",
        $("take").textContent);
  $("take").click();
  await shoot();
  check(posts().slice(-1)[0] === "POST upload?page=1&replace=1", "the retake replaces page 1", posts().slice(-1)[0]);
  check($("next").value === 5, "after it the number is the next free one", $("next").value);

  // 7. the stepper and a number that is not a page
  $("less").click();
  check($("next").value === 4 && $("take").textContent === "Retake page 4", "minus steps back", [$("next").value]);
  $("more").click();
  $("more").click();
  check($("next").value === 6 && $("take").textContent === "Take a page", "plus steps forward", [$("next").value]);
  $("next").value = "1.5";
  $("next").handlers.input();
  const before = $("camera").clicks;
  $("take").click();
  check($("camera").clicks === before && $("next").focused, "1.5 is not a page number");
  $("next").value = "5";
  $("next").handlers.input();

  // 8. states from the computer: done with the first lines, failed with the reason and a retake button
  setDone(1, "First line\nSecond line");
  setDone(2, "");
  Object.assign(server.status.pages.find(p => p.page === 3), { state: "failed", reason: "reply stopped: RECITATION" });
  Object.assign(server.status.pages.find(p => p.page === 4), { state: "reading" });
  server.status.stopped = "rate limit exceeded";
  $("take").click();
  await shoot();  // page 5, and with it a fresh status
  check(rows().map(r => r.page).join() === "5,4,3,2,1", "the newest page is on top", rows().map(r => r.page));
  check(row(1).state === "Done" && row(1).text === "First line\nSecond line", "a done page shows its first lines", row(1));
  check(row(2).text === "No text on this page.", "a blank page says so", row(2));
  check(row(3).state === "Failed" && row(3).text === "Reply stopped: RECITATION.", "a failed page shows its reason", row(3));
  check(row(4).state === "Reading" && row(1).thumb === "thumb/1?v=2", "reading state and a thumbnail per version",
        [row(4).state, row(1).thumb]);
  check(row(1).actions.length === 0 && row(3).actions.map(b => b.textContent).join() === "Retake",
        "only what needs a photo has a button in the list");
  check(!$("stopped").hidden && $("stopped").textContent.includes("rate limit exceeded"), "the stop is shown");
  const chips = $("summary").children.map(c => c.textContent);
  check(chips.join("|") === "2 read|2 in progress|1 failed · show", "the summary counts", chips);

  // 9. the failed filter
  $("summary").children[2].handlers.click();
  check(rows().map(r => r.page).join() === "3", "the filter leaves what failed", rows().map(r => r.page));
  check($("summary").children[2].textContent === "Show all pages", "and can be switched off");
  $("summary").children[2].handlers.click();
  check(rows().length === 5, "all pages are back");

  // 10. Retake in the list
  const beforeRetake = $("camera").clicks;
  action(3, "Retake").click();
  check($("camera").clicks === beforeRetake + 1, "Retake opens the camera");
  await shoot();
  check(posts().slice(-1)[0] === "POST upload?page=3&replace=1" && $("next").value === 6,
        "Retake replaces page 3 and leaves the number alone", [posts().slice(-1)[0], $("next").value]);

  // 11. tapping a page opens it: large photo, whole text, retake, neighbours, back
  server.texts[1] = "First line\nSecond line\n\nThe whole text of the page.";
  row(1).open.click();
  await settle();
  check(!$("view").hidden && body.classList.contains("viewing") && backs.length === 1, "the page opens");
  check($("viewnumber").textContent === "Page 1" && $("viewstate").textContent === "Done", "with its number and state");
  check($("viewphoto").children[0].src === "photo/1?v=2", "with the large photo", $("viewphoto").children[0].src);
  check($("viewtext").textContent === server.texts[1], "with the whole text", $("viewtext").textContent);
  check($("viewolder").disabled && !$("viewnewer").disabled, "page 1 has no page before it");
  $("viewphoto").handlers.click();
  check($("viewphoto").classList.contains("zoomed"), "a tap zooms the photo");
  $("viewnewer").click();
  await settle();
  check($("viewnumber").textContent === "Page 2" && $("viewtext").textContent === "No text on this page."
        && !$("viewphoto").classList.contains("zoomed"), "the next page", [$("viewnumber").textContent, $("viewtext").textContent]);
  $("viewolder").click();
  await settle();
  const beforeView = $("camera").clicks;
  $("viewretake").click();
  check($("camera").clicks === beforeView + 1, "Retake this page opens the camera");
  await shoot();
  check(posts().slice(-1)[0] === "POST upload?page=1&replace=1", "and replaces the page", posts().slice(-1)[0]);
  check($("viewstate").textContent === "Waiting" && $("viewtext").textContent === "Waiting for its turn to be read."
        && $("viewphoto").children[0].src === "photo/1?v=3", "the opened page follows the retake",
        [$("viewstate").textContent, $("viewtext").textContent, $("viewphoto").children[0].src]);
  check(!$("view").hidden, "the page stays open after the retake");
  backCalls = 0;
  $("viewclose").click();
  $("viewclose").click();  // an impatient second tap must not step out of the session
  await settle();
  check($("view").hidden && !body.classList.contains("viewing") && backs.length === 0, "back closes it");
  check(backCalls === 1, "one step back per opened page", backCalls);

  // 12. a status answer that was overtaken by a newer one is not shown
  let late;
  const realFetch = global.fetch;
  global.fetch = (url, options) => {
    if (url !== "status" || late) return realFetch(url, options);
    const old = JSON.parse(JSON.stringify(server.status));
    return new Promise(resolve => { late = () => resolve({ ok: true, status: 200, json: () => Promise.resolve(old) }); });
  };
  timers.pop()();                  // the regular poll asks for a status; its answer will be slow
  $("next").value = "8";
  $("next").handlers.input();
  $("take").click();
  await shoot();                   // page 8 is uploaded and a newer status shows it
  check(late && row(8) && row(8).state === "Waiting", "page 8 is listed from the newer status", rows().map(r => r.page));
  late();                          // now the slow answer arrives, from before page 8
  await settle();
  check(row(8) && row(8).state === "Waiting", "the late answer does not take page 8 away", rows().map(r => r.page));
  global.fetch = realFetch;

  // 13. basira was started again: the old address says so instead of "no connection"
  server.gone = true;
  timers.pop()();
  await settle();
  check(!$("expired").hidden && $("offline").hidden, "an address that is no longer in use is called that");
  server.gone = false;

  console.log(failures ? failures + " check(s) failed" : "page checks passed");
  process.exitCode = failures ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 1; });

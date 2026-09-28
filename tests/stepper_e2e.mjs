// Drives the version stepper in headless Chromium; tests/stepper_e2e.sh
// builds and serves exampleSite/ and passes its base URL. exampleSite's
// revised fragment has v1 and v2 pinned and an unpinned live v3; its first
// fragment's only version is both live and pinned, so it has nothing to
// step through.
import { createRequire } from "node:module";
import { execSync } from "node:child_process";

let playwright;
try {
  playwright = createRequire(import.meta.url)("playwright");
} catch {
  // A global install (npm i -g playwright), which ESM doesn't look in.
  playwright = createRequire(execSync("npm root -g").toString().trim() + "/")("playwright");
}

const base = process.argv[2];
const REVISED = "75znj6f4rhcm9skpr2009yeqtz";
const FIRST = "5cx94j6wbmzrdnnjxvs9j1nkba";
const origin = `${base}blyg/`;
const live = `${origin}f/${REVISED}/`;

let failed = 0;
function check(ok, what) {
  console.log(`${ok ? "ok" : "FAIL"}: ${what}`);
  if (!ok) failed++;
}

async function state(article) {
  return article.evaluate((a) => {
    const note = a.querySelector(".version-note");
    const extra = a.querySelector(".vextra");
    return {
      label: a.querySelector(".vlabel").textContent,
      content: a.querySelector(".item-content").textContent.replace(/\s+/g, " ").trim(),
      note: note ? note.textContent : null,
      noteHidden: note ? note.hidden : null,
      showingPin: a.classList.contains("showing-pin"),
      older: a.querySelector('.vstep[data-step="-1"]').disabled,
      newer: a.querySelector('.vstep[data-step="1"]').disabled,
      open: extra && extra.querySelector("a") ? extra.querySelector("a").href : null,
      latest: !!(extra && extra.querySelector(".vlatest")),
    };
  });
}

const browser = await playwright.chromium.launch();
try {
  // --- JavaScript on, on the item's own page -------------------------------
  const page = await browser.newPage();
  const fetched = [];
  page.on("request", (r) => {
    if (r.resourceType() === "fetch") fetched.push(r.url());
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(live);
  const article = page.locator("article").first();

  let s = await state(article);
  check(s.label === "v3" && !s.showingPin && s.newer && !s.older,
        `live page starts on the live v3, at the newest end (${JSON.stringify(s)})`);
  check(s.note === "“Revised again, not pinned”" && !s.noteHidden, "shows v3's note");
  check(s.content.startsWith("A fragment that gets revised. This third version"), "shows v3's body");
  check(await page.locator(".vnav .vstep").count() === 2, "adds the ‹ › stepper");

  await page.click('.vstep[data-step="-1"]');
  await page.waitForFunction(() => document.querySelector(".vlabel").textContent === "v2 · frozen");
  s = await state(article);
  check(s.showingPin && !s.older && !s.newer, "‹ steps to v2, marked frozen, as showing-pin");
  check(s.content.startsWith("A fragment that gets revised. This second version"), "swaps in v2's body");
  check(s.note === "“Revised, and pinned again”" && !s.noteHidden, "swaps in v2's note");
  check(s.open === `${live}v2/` && s.latest, "offers “open this version ↗” (v2's page) and “back to latest”");

  await page.click('.vstep[data-step="-1"]');
  await page.waitForFunction(() => document.querySelector(".vlabel").textContent === "v1 · frozen");
  s = await state(article);
  check(s.older && !s.newer, "‹ steps to v1, the oldest end");
  check(s.content.startsWith("A fragment that gets revised. This first version"), "swaps in v1's body");
  check(s.noteHidden === true, "hides the note (hidden, not emptied) for v1, which has none");

  await page.click(".vlatest");
  await page.waitForFunction(() => document.querySelector(".vlabel").textContent === "v3");
  s = await state(article);
  check(!s.showingPin && s.note === "“Revised again, not pinned”" && !s.noteHidden && !s.open,
        "“back to latest” restores v3, its note, and drops the pin links");

  // A plain click on a pin citation shows it in place rather than leaving.
  await page.click(`.pins a[href="${live}v1/"]`);
  await page.waitForFunction(() => document.querySelector(".vlabel").textContent === "v1 · frozen");
  check(page.url() === live, "clicking the v1 citation shows v1 in place");

  const pinned = new Set([1, 2].map((v) => `${origin}items/${REVISED}/v${v}.json`));
  check(fetched.length > 0 && fetched.every((u) => pinned.has(u)),
        `fetches only pinned versions' JSON (${fetched.join(", ")})`);
  check(errors.length === 0, `no script errors (${errors.join("; ")})`);

  // --- The feed page ----------------------------------------------------
  await page.goto(origin);
  const revised = page.locator("article", { has: page.locator(`[data-item="${REVISED}"]`) });
  check(await revised.locator(".vnav").count() === 1, "the feed page steps the revised fragment");
  const first = page.locator("article", { has: page.locator(`[data-item="${FIRST}"]`) });
  check(await first.locator(".vnav").count() === 0,
        "…but not the first fragment, whose one pin is its live version");
  check(await page.locator(".thread-card .vnav").count() === 0, "…nor a thread's card");
  await revised.locator('.vstep[data-step="-1"]').click();
  await page.waitForFunction(
    (id) => document.querySelector(`[data-item="${id}"] .vlabel`).textContent === "v2 · frozen", REVISED);
  check(true, "and steps it in place there");

  // --- A pinned page ------------------------------------------------------
  await page.goto(`${live}v1/`);
  check(await page.locator(".vnav").count() === 0 && await page.locator("script[src*=version-nav]").count() === 0,
        "a pinned page runs no stepper");
  check((await page.locator(".vlabel").textContent()) === "v1 · frozen", "and says it's frozen in text");
  await page.close();

  // --- JavaScript off -----------------------------------------------------
  const context = await browser.newContext({ javaScriptEnabled: false });
  const nojs = await context.newPage();
  await nojs.goto(live);
  check(await nojs.locator(".vnav").count() === 0, "no stepper without JavaScript");
  for (const v of [1, 2]) {
    await nojs.goto(live);
    const response = await Promise.all([
      nojs.waitForNavigation(),
      nojs.click(`.pins a[href="${live}v${v}/"]`),
    ]).then(([r]) => r);
    check(response && response.status() === 200 && nojs.url() === `${live}v${v}/`
          && (await nojs.locator(".pinned-banner").count()) === 1,
          `without JavaScript, the v${v} citation is a plain link to its pinned page`);
  }
  await context.close();
} finally {
  await browser.close();
}

if (failed) {
  console.error(`${failed} check(s) failed`);
  process.exit(1);
}

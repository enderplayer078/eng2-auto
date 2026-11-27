"""Multi-section automated runner for ENG2 (Keynote 3 on learn.eltngl.com)."""
import argparse
import json
import random
import re
import shutil
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from account_session import open_session, save_session, session_folder
import answer_guard
import solver

ASSIGNMENTS_HOME = "https://learn.eltngl.com/dashboard/assignments"
KEYNOTE3_RE = re.compile(r"keynote\s*3|english\s*2|\beng\s*2\b", re.IGNORECASE)
UNIT_PREFIX_RE = re.compile(r"^\s*(Unit\s+\d+)\b", re.IGNORECASE)


def _extract_unit_label(name: str) -> str:
    m = UNIT_PREFIX_RE.match(str(name or ""))
    return m.group(1).strip().lower() if m else ""


def _validate_ngl_url(url: str) -> str:
    if not isinstance(url, str) or not url.startswith("https://learn.eltngl.com/"):
        raise RuntimeError("Unexpected assignment URL host")
    return url


def discover_assignment_urls(driver) -> list[str]:
    """Find active Keynote 3 assignment URLs across any section; fallback to all active cards."""
    driver.switch_to.default_content()
    driver.get(ASSIGNMENTS_HOME)
    WebDriverWait(driver, 30).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, '[data-qa-id="assignmentCard"]')
    )
    time.sleep(1.5)

    cards = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('[data-qa-id="assignmentCard"]')).map(card => {
            const section = card.closest('[data-qa-id="sec-assignments_course-section"]');
            const courseTitleEl = section
                ? section.querySelector('[data-qa-id="sec-assignments_course-preview_title"]')
                : null;
            const cardTitleEl = card.querySelector('[data-qa-id="sec-assignments_card-title"]');
            const linkEl = card.closest('a[href]') || card.querySelector('a[href]');
            return {
                courseTitle: courseTitleEl ? (courseTitleEl.innerText || '').trim() : '',
                cardTitle: cardTitleEl ? (cardTitleEl.innerText || '').trim() : '',
                cardText: (card.innerText || '').trim().replace(/\\s+/g, ' '),
                href: linkEl ? (linkEl.href || '') : ''
            };
        });
        """
    )

    all_urls: list[str] = []
    matched_urls: list[str] = []
    seen_all: set[str] = set()
    seen_matched: set[str] = set()

    for c in cards or []:
        href = c.get("href") or ""
        if not href.startswith("https://learn.eltngl.com/"):
            continue
        url = _validate_ngl_url(href)
        if url not in seen_all:
            seen_all.add(url)
            all_urls.append(url)
        combined = f"{c.get('courseTitle', '')} {c.get('cardTitle', '')} {c.get('cardText', '')}"
        if KEYNOTE3_RE.search(combined) and url not in seen_matched:
            seen_matched.add(url)
            matched_urls.append(url)

    selected = matched_urls if matched_urls else all_urls
    if not selected:
        raise RuntimeError("No active assignment card found on dashboard")
    return selected


def load_dashboard_rows(driver, url: str) -> list[dict]:
    driver.switch_to.default_content()
    driver.get(url)
    WebDriverWait(driver, 30).until(
        lambda d: len(d.find_elements(By.CSS_SELECTOR, '[data-qa-id="activityRow"]')) > 0
    )
    time.sleep(2)
    if "/dashboard" not in driver.current_url.lower():
        raise RuntimeError("Session expired; log in again before resuming")
    return driver.execute_script(
        """
        return Array.from(document.querySelectorAll('[data-qa-id="activityRow"]')).map((row, idx) => {
            const titleEl = row.querySelector('span[title]');
            const name = titleEl ? titleEl.getAttribute('title').trim() : '';
            const text = (row.innerText || '').trim();
            return { idx, name, text };
        });
        """
    )


def is_row_completed(row: dict) -> bool:
    text = row.get("text", "")
    if re.search(r"Score\s+100\s*%", text):
        return True
    m = re.search(r"Score\s+(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", text)
    if m:
        num, den = float(m.group(1)), float(m.group(2))
        return den > 0 and num >= den
    return False


def is_row_attempts_exhausted(row: dict) -> bool:
    m = re.search(r"Attempts\s+(\d+)\s*/\s*(\d+)", row.get("text", ""))
    if m:
        used, allowed = int(m.group(1)), int(m.group(2))
        return allowed > 0 and used >= allowed
    return False


def verify_saved(driver, url: str, name: str) -> list[dict]:
    for delay in (3, 6, 12):
        rows = load_dashboard_rows(driver, url)
        match = next((r for r in rows if r["name"] == name), None)
        if match and is_row_completed(match):
            return rows
        time.sleep(delay)
    raise RuntimeError(f"Dashboard did not confirm 100% completion for: {name}")


def _paced_sleep(driver, seconds: float, heartbeat_callback=None) -> None:
    """Sleep in chunks while firing mousemove heartbeats inside the active frame to keep idleTime=0."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    next_hb = time.monotonic() + random.uniform(12.0, 18.0)
    next_active_ping = time.monotonic() + 45.0
    while True:
        now = time.monotonic()
        rem = deadline - now
        if rem <= 0:
            break
        time.sleep(min(rem, 3.0))
        now = time.monotonic()
        if now >= next_hb:
            try:
                driver.execute_script(
                    """
                    const x = Math.floor(180 + Math.random() * 520);
                    const y = Math.floor(140 + Math.random() * 340);
                    window.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: x, clientY: y }));
                    document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: x, clientY: y }));
                    """
                )
            except Exception:
                pass
            next_hb = now + random.uniform(12.0, 18.0)
        if heartbeat_callback and now >= next_active_ping:
            try:
                heartbeat_callback()
            except Exception:
                pass
            next_active_ping = now + 45.0


def _build_activity_budgets(todo_names: list[str], target_total_minutes: float) -> dict[str, float]:
    """Distribute target_total_minutes across remaining activities with Lesson D weighting."""
    count = len(todo_names)
    if count <= 0 or target_total_minutes <= 0:
        return {name: 0.0 for name in todo_names}
    total_active_sec = max(0.0, target_total_minutes * 60.0 - count * 7.0)
    if total_active_sec <= 0:
        return {name: 0.0 for name in todo_names}
    weights = []
    for name in todo_names:
        if "lesson d" in name.lower():
            weights.append(1.35 * random.uniform(0.85, 1.15))
        else:
            weights.append(random.uniform(0.75, 1.25))
    w_sum = sum(weights) or 1.0
    return {name: (w / w_sum) * total_active_sec for name, w in zip(todo_names, weights)}


def _flush_activity_close(driver) -> None:
    """Trigger Avallain xAPI terminated statement (keepalive) cleanly after submit."""
    try:
        driver.execute_script(
            """
            try {
                if (window.a5 && window.a5.mainBuilder && typeof window.a5.mainBuilder.updateDuration === 'function') {
                    window.a5.mainBuilder.updateDuration();
                }
                if (window.a5 && window.a5.service && window.a5.service.lms && window.a5.service.lms.API && typeof window.a5.service.lms.API.close === 'function') {
                    window.a5.service.lms.API.close();
                }
            } catch (_) {}
            """
        )
    except Exception:
        pass


def run_activity(
    driver,
    row: dict,
    quiet_logs: bool = False,
    activity_budget_seconds: float = 0.0,
    heartbeat_callback=None,
) -> None:
    name = row["name"]
    driver.switch_to.default_content()
    clicked = driver.execute_script(
        """
        const targetName = arguments[0];
        const rows = Array.from(document.querySelectorAll('[data-qa-id="activityRow"]'));
        const row = rows.find(r => {
            const el = r.querySelector('span[title]');
            return el && el.getAttribute('title').trim() === targetName;
        });
        if (!row) return false;
        row.scrollIntoView({ block: 'center' });
        row.click();
        return true;
        """,
        name,
    )
    if not clicked:
        raise RuntimeError(f"Activity row not found: {name}")

    if not solver.switch_to_inner_frame(driver, timeout=35):
        raise RuntimeError(f"Activity inner frame did not load for: {name}")

    # Ensure we start from the first interaction screen
    driver.execute_script(
        """
        const mb = window.a5.mainBuilder;
        const list = mb.getInteractionsWithoutEndResult();
        if (list.length > 0 && mb.curInteraction !== list[0]) {
            mb.goTo(list[0].index);
        }
        """
    )
    time.sleep(0.5)

    info = solver.get_screen_info(driver)
    total_screens = info["totalScreens"]
    activity_started = time.monotonic()

    per_screen_wait = 0.0
    if activity_budget_seconds > 0 and total_screens > 0:
        per_screen_wait = (activity_budget_seconds * 0.8) / total_screens

    for s_idx in range(total_screens):
        WebDriverWait(driver, 20, poll_frequency=0.25).until(
            lambda d: d.execute_script(
                "return Boolean(window.a5 && window.a5.mainBuilder && window.a5.mainBuilder.curInteraction && window.a5.mainBuilder.curInteraction.buildDone);"
            )
        )
        cur_info = solver.get_screen_info(driver)
        if not quiet_logs:
            print(
                f"  [{name}] screen {s_idx + 1}/{total_screens} ({cur_info['identifier']})",
                flush=True,
            )

        if per_screen_wait > 0:
            _paced_sleep(driver, per_screen_wait * random.uniform(0.8, 1.2), heartbeat_callback)

        solver.solve_current_screen(driver)
        answer_guard.verify_screen(driver)

        if s_idx < total_screens - 1:
            prev_idx = cur_info["index"]
            driver.execute_script("return window.a5.mainBuilder.goNext();")
            WebDriverWait(driver, 20, poll_frequency=0.25).until(
                lambda d: d.execute_script(
                    "const c = window.a5.mainBuilder.curInteraction; return Boolean(c && c.buildDone && c.index !== arguments[0]);",
                    prev_idx,
                )
            )

    all_screens = answer_guard.verify_all_screens_before_submit(driver)
    total_pts = sum(s["total"] for s in all_screens)
    correct_pts = sum(s["correct"] for s in all_screens)
    if not quiet_logs:
        print(
            f"  [{name}] pre-submit verified: {correct_pts}/{total_pts} across {len(all_screens)} screens",
            flush=True,
        )

    if activity_budget_seconds > 0:
        rem_in_activity = activity_budget_seconds - (time.monotonic() - activity_started)
        if rem_in_activity > 0:
            _paced_sleep(driver, rem_in_activity, heartbeat_callback)

    submit_res = driver.execute_async_script(
        """
        const done = arguments[arguments.length - 1];
        const mb = window.a5.mainBuilder;
        let submitPayload = null;
        const onSubmit = function(data) {
            submitPayload = {
                correct: data && data.score ? data.score.correct() : null,
                total: data && data.score ? data.score.total() : null,
                scaled: data && data.score ? data.score.scaled() : null,
                percent: data && data.score ? data.score.percent() : null
            };
        };
        window.a5.eventBus.bind('lo:submit', onSubmit);
        $.when(mb.submit({ noConfirm: true }))
            .done(function() {
                setTimeout(function() {
                    window.a5.eventBus.unbind('lo:submit', onSubmit);
                    const r = mb.resultsGenerator && mb.resultsGenerator.getResults ? mb.resultsGenerator.getResults() : null;
                    done({
                        ok: true,
                        submitPayload: submitPayload,
                        generator: r ? {
                            correct: r.correct(),
                            total: r.total(),
                            scaled: r.scaled(),
                            percent: r.percent()
                        } : null
                    });
                }, 1500);
            })
            .fail(function(err) {
                window.a5.eventBus.unbind('lo:submit', onSubmit);
                done({ ok: false, error: String(err) });
            });
        """
    )
    if not quiet_logs:
        print(f"  RESULT {name}: {submit_res}", flush=True)
    if not submit_res or not submit_res.get("ok"):
        raise RuntimeError(f"Submit failed for {name}")

    score_info = submit_res.get("submitPayload") or submit_res.get("generator")
    if not score_info or score_info["correct"] != score_info["total"] or score_info["percent"] != 100:
        raise RuntimeError(f"Score needs review for {name}")

    _flush_activity_close(driver)
    time.sleep(2)


def run_remaining(
    driver,
    folder: Path,
    username: str,
    filter_text: str | None = None,
    min_activity_seconds: float = 0.0,
    progress_callback=None,
    heartbeat_callback=None,
    target_total_minutes: float = 0.0,
    max_units: int = 0,
    max_activities: int = 0,
    persist_session: bool = False,
    quiet_logs: bool = False,
) -> dict:
    urls = discover_assignment_urls(driver)
    rows_by_url: list[tuple[str, list[dict]]] = []
    for url in urls:
        rows_by_url.append((url, load_dashboard_rows(driver, url)))

    all_rows = [r for _, rows in rows_by_url for r in rows]
    total_count = len(all_rows)
    done_count = sum(1 for r in all_rows if is_row_completed(r) or is_row_attempts_exhausted(r))
    if not quiet_logs:
        print(f"Loaded {total_count} activities across {len(urls)} assignment(s)", flush=True)
    if progress_callback:
        progress_callback(done_count, total_count)

    candidate_items: list[tuple[str, str]] = []
    for url_idx, (_, rows) in enumerate(rows_by_url):
        for r in rows:
            if (
                (not filter_text or filter_text in r["name"])
                and not is_row_completed(r)
                and not is_row_attempts_exhausted(r)
            ):
                candidate_items.append((f"{url_idx}::{r['name']}", r["name"]))

    selected_keys_list: list[str] = []
    if max_units and max_units > 0:
        selected_units: list[str] = []
        has_unit_labels = any(_extract_unit_label(nm) for _, nm in candidate_items)
        if has_unit_labels:
            for key, nm in candidate_items:
                ulab = _extract_unit_label(nm)
                if ulab:
                    if ulab not in selected_units:
                        if len(selected_units) >= max_units:
                            continue
                        selected_units.append(ulab)
                    selected_keys_list.append(key)
        else:
            selected_keys_list = [k for k, _ in candidate_items[: max_units * 10]]
    else:
        selected_keys_list = [k for k, _ in candidate_items]

    if max_activities and max_activities > 0:
        selected_keys_list = selected_keys_list[:max_activities]

    selected_key_set = set(selected_keys_list)
    is_limited_run = bool((max_units and max_units > 0) or (max_activities and max_activities > 0))

    # Scale pacing duration proportionally so partial runs + remaining runs add up naturally
    effective_target_minutes = float(target_total_minutes or 0.0)
    if effective_target_minutes > 0 and total_count > 0 and len(selected_keys_list) < total_count:
        effective_target_minutes = effective_target_minutes * (len(selected_keys_list) / float(total_count))

    budgets = _build_activity_budgets(selected_keys_list, effective_target_minutes)
    ran_this_session = 0

    for url_idx, (url, rows) in enumerate(rows_by_url):
        rows = load_dashboard_rows(driver, url)
        rows_by_url[url_idx] = (url, rows)
        for row in rows:
            name = row["name"]
            key = f"{url_idx}::{name}"
            if key not in selected_key_set:
                continue
            if filter_text and filter_text not in name:
                continue
            if is_row_completed(row) or is_row_attempts_exhausted(row):
                continue

            if not quiet_logs:
                print(f"START {name}", flush=True)
            started = time.monotonic()
            activity_budget = max(float(min_activity_seconds or 0), float(budgets.get(key, 0.0)))

            for attempt in range(1, 4):
                try:
                    run_activity(
                        driver,
                        row,
                        quiet_logs=quiet_logs,
                        activity_budget_seconds=activity_budget,
                        heartbeat_callback=heartbeat_callback,
                    )
                    rows = verify_saved(driver, url, name)
                    rows_by_url[url_idx] = (url, rows)
                    break
                except Exception as exc:
                    if quiet_logs:
                        print(f"RETRY {attempt}/3: {type(exc).__name__}", flush=True)
                    else:
                        print(f"RETRY {attempt}/3 {name}: {type(exc).__name__}", flush=True)
                    if attempt == 3:
                        if persist_session:
                            try:
                                driver.switch_to.default_content()
                                (folder / "failure.html").write_text(driver.page_source)
                                driver.save_screenshot(str(folder / "failure.png"))
                            except Exception:
                                pass
                        raise
                    time.sleep(4)
                    rows = load_dashboard_rows(driver, url)
                    rows_by_url[url_idx] = (url, rows)
                    latest = next((r for r in rows if r["name"] == name), None)
                    if latest and (is_row_completed(latest) or is_row_attempts_exhausted(latest)):
                        break
                    if latest:
                        row = latest

            ran_this_session += 1
            if persist_session:
                save_session(driver, username, folder)

            all_rows = [r for _, rlist in rows_by_url for r in rlist]
            done_count = sum(1 for r in all_rows if is_row_completed(r) or is_row_attempts_exhausted(r))
            if not quiet_logs:
                print(f"VERIFIED 100% on dashboard: {name}", flush=True)
            if progress_callback:
                progress_callback(done_count, total_count)

            remaining = min_activity_seconds - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)

    final_rows: list[dict] = []
    for url, _ in rows_by_url:
        final_rows.extend(load_dashboard_rows(driver, url))

    completed_count = sum(1 for r in final_rows if is_row_completed(r) or is_row_attempts_exhausted(r))
    full_score_count = sum(1 for r in final_rows if is_row_completed(r))
    all_completed = completed_count >= len(final_rows)
    if not filter_text and not is_limited_run and not all_completed:
        raise RuntimeError(f"Incomplete activities remain ({completed_count}/{len(final_rows)})")
    if progress_callback:
        progress_callback(completed_count, len(final_rows))
    return {
        "total": len(final_rows),
        "completed": completed_count,
        "graded_full_score": full_score_count,
        "all_completed": all_completed,
        "partial": not all_completed,
        "ran_this_session": ran_this_session,
    }


def run_for_account(
    username: str,
    password: str,
    headless: bool = True,
    quiet_logs: bool = True,
    on_progress=None,
    on_heartbeat=None,
    min_total_minutes: float = 20.0,
    max_total_minutes: float = 40.0,
    max_units: int = 0,
    max_activities: int = 0,
) -> dict:
    """Run remaining ENG2 activities for one account with guaranteed profile cleanup."""
    folder = session_folder(username)
    driver = None
    try:
        target_minutes = 0.0 if max_total_minutes <= 0 else random.uniform(min_total_minutes, max_total_minutes)
        driver, folder = open_session(
            username,
            password=password,
            headed=not headless,
            persist_session=False,
        )
        return run_remaining(
            driver,
            folder,
            username,
            progress_callback=on_progress,
            heartbeat_callback=on_heartbeat,
            target_total_minutes=target_minutes,
            max_units=max_units,
            max_activities=max_activities,
            persist_session=False,
            quiet_logs=quiet_logs,
        )
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run NGL Spark ENG2 assignments for an account")
    parser.add_argument("username")
    parser.add_argument("filter_text", nargs="?")
    parser.add_argument(
        "--min-activity-seconds",
        type=float,
        default=0,
        help="Minimum wall-clock time per activity, including post-save waiting",
    )
    parser.add_argument(
        "--max-units",
        type=int,
        default=0,
        help="Optional limit on number of incomplete units to complete (0 = all)",
    )
    parser.add_argument(
        "--max-activities",
        type=int,
        default=0,
        help="Optional hard limit on number of activities to complete (0 = all)",
    )
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    if not 0 <= args.min_activity_seconds <= 3600:
        parser.error("Minimum activity seconds must be between 0 and 3600")

    d, f = open_session(args.username, headed=args.headed, persist_session=True)
    try:
        summary = run_remaining(
            d,
            f,
            args.username,
            filter_text=args.filter_text,
            min_activity_seconds=args.min_activity_seconds,
            max_units=max(0, args.max_units),
            max_activities=max(0, args.max_activities),
            persist_session=True,
            quiet_logs=False,
        )
        print("SUMMARY:", summary, flush=True)
    finally:
        d.quit()

/**
* POLO — polo_record.js
* Playwright screen recorder for demo video generation
* Usage: node polo_record.js --app groomeros
* Requires: npm install playwright
*/
const { chromium } = require('playwright');
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

// ── LOAD CONFIG ──────────────────────────────────────────────
const config = JSON.parse(fs.readFileSync('./polo_config.json', 'utf8'));

const args = process.argv.slice(2);
const appArg = args.find(a => a.startsWith('--app='))?.split('=')[1]
            || args[args.indexOf('--app') + 1];

if (!appArg || !config.apps[appArg]) {
  console.error('[POLO ERROR] App not specified or not found in config.');
  console.error('Usage: node polo_record.js --app groomeros');
  console.error('Available apps:', Object.keys(config.apps).join(', '));
  process.exit(1);
}

const app = config.apps[appArg];
const rec = config.recording;
const out = config.output;

if (app.status === 'WAITING_ON_BUILD') {
  console.error(`[POLO BLOCKED] ${app.name} is waiting on build completion. Cannot record.`);
  process.exit(1);
}

if (app.status === 'WAITING_ON_TRUTH') {
  console.warn(`[POLO WARNING] ${app.name} has not been verified by TRUTH yet.`);
  console.warn('TRUTH must confirm: signup → payment → Pro unlock before recording.');
  console.warn('Proceeding only if you have manually confirmed the funnel works.');
  console.warn('Add --force flag to proceed anyway.');
  if (!args.includes('--force')) process.exit(1);
}

// ── OUTPUT PATH ───────────────────────────────────────────────

if (!fs.existsSync(out.output_dir)) {
  fs.mkdirSync(out.output_dir, { recursive: true });
}

const date = new Date().toISOString().split('T')[0].replace(/-/g, '');
const version = app.current_version || 'v1';
const videoPath = path.join(
  out.output_dir,
  out.filename_template
    .replace('{app}', app.name.replace(/\s/g, ''))
    .replace('{version}', version)
    .replace('{date}', date)
    .replace('.mp4', '_raw.webm') // raw recording before FFmpeg
);

// ── MAIN RECORD ───────────────────────────────────────────────
(async () => {
  console.log(`\n[POLO] Starting recording for ${app.name}`);
  console.log(`[POLO] Target URL: ${app.url}`);
  console.log(`[POLO] Output: ${videoPath}\n`);

  const browser = await chromium.launch({
    headless: rec.headless,
    slowMo: rec.slow_mo_ms,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });

  const context = await browser.newContext({
    viewport: { width: rec.viewport_width, height: rec.viewport_height },
    recordVideo: {
      dir: out.output_dir,
      size: { width: rec.viewport_width, height: rec.viewport_height }
    }
  });

  const page = await context.newPage();

  try {
    // ── EXECUTE INTERACTIONS ──────────────────────────────────
    for (const step of app.interactions) {

      switch (step.action) {

        case 'navigate':
          if (step.target === 'homepage') {
            console.log(`[POLO] Navigating to homepage: ${app.url}`);
            await page.goto(app.url, { waitUntil: 'networkidle', timeout: 30000 });

            // ── FIX: Wait for page to fully render before recording captures frames
            console.log('[POLO] Waiting 3s for page render...');
            await page.waitForTimeout(3000);

            // ── FIX: Pre-record screenshot to confirm page is visible (not black)
            const preRecordScreenshot = path.join(
              out.output_dir,
              `POLO_${app.name.replace(/\s/g, '')}_${version}_${date}_prerecord.png`
            );
            await page.screenshot({ path: preRecordScreenshot, fullPage: false });
            console.log(`[POLO] Pre-record screenshot saved: ${preRecordScreenshot}`);

          } else if (step.target === 'pricing') {
            // Try common pricing URLs
            const pricingUrls = [
              `${app.url}/pricing`,
              `${app.url}/plans`,
              `${app.url}/subscribe`
            ];
            let navigated = false;
            for (const url of pricingUrls) {
              try {
                await page.goto(url, { waitUntil: 'networkidle', timeout: 10000 });
                navigated = true;
                console.log(`[POLO] Navigated to pricing: ${url}`);
                break;
              } catch (_) { continue; }
            }
            if (!navigated) {
              console.warn('[POLO] Could not find pricing page — staying on current page');
            }
          }
          break;

        case 'pause':
          console.log(`[POLO] Pausing ${step.ms}ms — ${step.note || ''}`);
          await page.waitForTimeout(step.ms);
          break;

        case 'scroll':
          console.log(`[POLO] Scrolling ${step.direction} ${step.amount}px`);
          await page.evaluate(({ dir, amount }) => {
            window.scrollBy({
              top: dir === 'down' ? amount : -amount,
              behavior: 'smooth'
            });
          }, { dir: step.direction, amount: step.amount });
          await page.waitForTimeout(800);
          break;

        case 'click':
          console.log(`[POLO] Clicking: ${step.note || step.selector}`);
          try {
            await page.click(step.selector, { timeout: 5000 });
            await page.waitForTimeout(500);
          } catch (e) {
            console.warn(`[POLO] Could not click selector: ${step.selector} — skipping`);
          }
          break;

        case 'type':
          console.log(`[POLO] Typing: "${step.text}"`);
          await page.keyboard.type(step.text, { delay: 80 });
          await page.waitForTimeout(1000);
          break;

        case 'hover':
          console.log(`[POLO] Hovering: ${step.note || step.selector}`);
          try {
            await page.hover(step.selector, { timeout: 5000 });
            await page.waitForTimeout(500);
          } catch (e) {
            console.warn(`[POLO] Could not hover selector: ${step.selector} — skipping`);
          }
          break;

        case 'end':
          console.log('[POLO] Interaction sequence complete.');
          break;

        default:
          console.warn(`[POLO] Unknown action: ${step.action} — skipping`);
      }
    }

    // ── CAPTURE THUMBNAIL ─────────────────────────────────────
    // Wait 3 seconds in, then screenshot for thumbnail
    const thumbPath = path.join(
      out.output_dir,
      `POLO_${app.name.replace(/\s/g, '')}_${version}_${date}_thumb.png`
    );
    await page.screenshot({ path: thumbPath, fullPage: false });
    console.log(`[POLO] Thumbnail saved: ${thumbPath}`);

    // ── CLOSE + SAVE VIDEO ────────────────────────────────────
    await context.close();
    await browser.close();

    // Playwright saves video with auto-generated name — find and rename it
    const files = fs.readdirSync(out.output_dir)
      .filter(f => f.endsWith('.webm') && !f.startsWith('POLO_'));

    if (files.length > 0) {
      const rawFile = path.join(out.output_dir, files[0]);
      fs.renameSync(rawFile, videoPath);
      console.log(`[POLO] Raw recording saved: ${videoPath}`);
    }

    // ── WRITE SESSION LOG ─────────────────────────────────────
    const sessionLog = {
      app: app.name,
      version,
      date,
      raw_video: videoPath,
      thumbnail: thumbPath,
      status: 'RECORDED_RAW',
      next_step: 'Run polo_voiceover.js then polo_render.sh'
    };

    const logPath = path.join(out.output_dir, `polo_session_${appArg}_${date}.json`);
    fs.writeFileSync(logPath, JSON.stringify(sessionLog, null, 2));

    console.log('\n[POLO] ✓ Recording complete');
    console.log('[POLO] Session log:', logPath);
    console.log('[POLO] Next: node polo_voiceover.js --app', appArg);

  } catch (err) {
    await browser.close();
    console.error('[POLO ERROR]', err.message);
    console.error('[POLO VERDICT] RETRY — re-run once. If it fails again, escalate to CLAUDER.');
    process.exit(1);
  }
})();

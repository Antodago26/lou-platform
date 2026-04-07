"""
Lou Garou — Cron Job
Triggers centralized scraping via HTTP endpoint.

Run every 2 hours via Render Cron Job:
  Command: python cron_job.py
  Schedule: 0 */2 * * *
  Env: CRON_SECRET, RENDER_APP_URL (or defaults to https://garou.ch)
"""

import os
import sys
import json
import logging
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
log = logging.getLogger('lou-cron')

APP_URL = os.environ.get('RENDER_APP_URL', 'https://garou.ch')
CRON_SECRET = os.environ.get('CRON_SECRET', '')


def run():
    if not CRON_SECRET:
        log.error("CRON_SECRET not set")
        sys.exit(1)

    url = f"{APP_URL}/api/cron/scrape"
    log.info(f"Triggering scrape: {url}")

    try:
        r = requests.post(
            url,
            headers={'X-Cron-Secret': CRON_SECRET},
            timeout=300
        )
        data = r.json()
        log.info(f"Response ({r.status_code}): {json.dumps(data, indent=2)}")

        if data.get('ok'):
            log.info(f"Success: {data.get('total_scraped', 0)} scraped, "
                     f"{data.get('total_saved', 0)} saved, "
                     f"{data.get('total_scored', 0)} scored in {data.get('time_s', 0)}s")
        else:
            log.error(f"Error: {data.get('error', 'unknown')}")
            sys.exit(1)

    except Exception as e:
        log.error(f"Cron job failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    run()

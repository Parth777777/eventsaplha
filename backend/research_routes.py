"""Equity research routes — /api/research/<ticker>.

Free users get sections 1, 2, 8 + a 1-sentence thesis preview (rest locked).
Starter unlocks the full 8-section deep-dive at 1/day.
Pro gets 20/day with priority queue.

Daily-cap is enforced at request time using research_usage table.
24h server-side cache means repeat hits in the same day are free.

Registration:
    from backend.research_routes import register
    register(app, get_db, optional_auth, require_auth)
"""
from __future__ import annotations

import logging
from typing import Callable

from flask import g, jsonify, request

logger = logging.getLogger(__name__)


def register(app, get_db: Callable, optional_auth, require_auth):
    @app.route('/api/research/<ticker>', methods=['GET'])
    @optional_auth
    def get_research(ticker):
        from backend.equity_research import (
            build_report, apply_tier_visibility,
            count_today, record_usage,
        )
        from backend.freemium import get_user_tier, research_daily_cap

        db = get_db()
        user_id = str(g.user_id) if (g.user_id and g.user_id != 'legacy') else 'anon'
        tier = get_user_tier(db, user_id) if user_id != 'anon' else 'free'
        cap = research_daily_cap(tier)
        used = count_today(db, user_id) if user_id != 'anon' else 0

        # Free + anon: always allowed to fetch the preview (no daily cap).
        # Starter: 1/day full report. Pro: 20/day.
        wants_full = tier in ('starter', 'pro')
        if wants_full and cap > 0 and used >= cap:
            return jsonify({
                'success': False,
                'error': 'research_quota_exceeded',
                'tier': tier,
                'limit': cap,
                'used_today': used,
                'upgrade_url': '/app/pricing.html' if tier != 'pro' else None,
            }), 429

        # Build (or fetch cached) report — narrative included only for paid tiers
        report = build_report(db, ticker, include_narrative=wants_full)
        if 'error' in report:
            return jsonify({'success': False, 'error': report['error']}), 400

        # Apply tier-aware visibility (mutates report in place)
        apply_tier_visibility(report, tier)

        # Record usage only if it cost us narrative tokens (full report served)
        if wants_full and not report.get('cached'):
            record_usage(db, user_id, ticker.upper())

        report['success'] = True
        report['remaining_today'] = max(0, cap - used - (1 if wants_full and not report.get('cached') else 0))
        return jsonify(report)

    @app.route('/api/research/<ticker>/preview', methods=['GET'])
    @optional_auth
    def get_research_preview(ticker):
        """Lightweight free preview — never burns Groq tokens. Used by the
        stock.html unauthenticated initial render."""
        from backend.equity_research import build_report, apply_tier_visibility

        db = get_db()
        report = build_report(db, ticker, include_narrative=False)
        if 'error' in report:
            return jsonify({'success': False, 'error': report['error']}), 400
        apply_tier_visibility(report, 'free')
        report['success'] = True
        return jsonify(report)

    logger.info("research_routes: registered /api/research/<ticker> + /preview")

"""Saved custom screeners — Starter (5) and Pro (25) only. Free 402s.

Table: saved_screeners(id, user_id, name, filter_json, created_at, updated_at)

Endpoints:
    GET    /api/screeners/saved       — list current user's saved screeners
    POST   /api/screeners/saved       — create new (body: {name, filter})
    PUT    /api/screeners/saved/<id>  — rename or update filter
    DELETE /api/screeners/saved/<id>

Registration:
    from backend.saved_screeners import register
    register(app, get_db, require_auth)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Callable

from flask import g, jsonify, request

logger = logging.getLogger(__name__)

_SCHEMA_ENSURED = False


def ensure_schema(db) -> None:
    global _SCHEMA_ENSURED
    if _SCHEMA_ENSURED:
        return
    try:
        cur = db.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_screeners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                filter_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_saved_screeners_user "
            "ON saved_screeners(user_id, created_at)"
        )
        db.conn.commit()
        _SCHEMA_ENSURED = True
    except Exception as e:
        logger.warning("saved_screeners schema ensure failed: %s", e)


def _row_to_dict(r, fields):
    try:
        return dict(r)
    except (TypeError, ValueError):
        return {f: r[i] for i, f in enumerate(fields)}


def register(app, get_db: Callable, require_auth):
    """Wire the 4 endpoints. All require auth + tier check."""

    @app.route('/api/screeners/saved', methods=['GET'])
    @require_auth
    def list_saved():
        db = get_db()
        ensure_schema(db)
        from backend.freemium import get_user_tier, saved_screeners_cap
        tier = get_user_tier(db, g.user_id)
        cap = saved_screeners_cap(tier)
        try:
            cur = db.conn.cursor()
            rows = cur.execute(
                "SELECT id, name, filter_json, created_at, updated_at "
                "FROM saved_screeners WHERE user_id = ? "
                "ORDER BY updated_at DESC",
                (str(g.user_id),),
            ).fetchall()
            fields = ['id', 'name', 'filter_json', 'created_at', 'updated_at']
            items = []
            for r in rows:
                d = _row_to_dict(r, fields)
                try:
                    d['filter'] = json.loads(d.pop('filter_json'))
                except Exception:
                    d['filter'] = {}
                items.append(d)
            return jsonify({
                'success': True,
                'tier': tier,
                'limit': cap,
                'current': len(items),
                'data': items,
            })
        except Exception as e:
            logger.error("list_saved screeners failed: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/screeners/saved', methods=['POST'])
    @require_auth
    def create_saved():
        db = get_db()
        ensure_schema(db)
        from backend.freemium import check_saved_screeners_limit
        err = check_saved_screeners_limit(db, g.user_id)
        if err:
            return jsonify(err), 402

        body = request.get_json(silent=True) or {}
        name = (body.get('name') or '').strip()
        flt = body.get('filter') or {}
        if not name or not isinstance(flt, dict):
            return jsonify({'success': False, 'error': 'name + filter required'}), 400
        try:
            cur = db.conn.cursor()
            cur.execute(
                "INSERT INTO saved_screeners (user_id, name, filter_json) "
                "VALUES (?, ?, ?)",
                (str(g.user_id), name[:120], json.dumps(flt)),
            )
            db.conn.commit()
            return jsonify({'success': True, 'id': cur.lastrowid}), 201
        except Exception as e:
            logger.error("create_saved screener failed: %s", e)
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/screeners/saved/<int:sid>', methods=['PUT'])
    @require_auth
    def update_saved(sid):
        db = get_db()
        ensure_schema(db)
        body = request.get_json(silent=True) or {}
        sets = []
        params = []
        if 'name' in body:
            sets.append('name = ?')
            params.append((body.get('name') or '').strip()[:120])
        if 'filter' in body:
            try:
                sets.append('filter_json = ?')
                params.append(json.dumps(body.get('filter') or {}))
            except Exception:
                return jsonify({'success': False, 'error': 'invalid filter'}), 400
        if not sets:
            return jsonify({'success': False, 'error': 'nothing to update'}), 400
        sets.append("updated_at = CURRENT_TIMESTAMP")
        params.extend([sid, str(g.user_id)])
        try:
            cur = db.conn.cursor()
            cur.execute(
                f"UPDATE saved_screeners SET {', '.join(sets)} "
                f"WHERE id = ? AND user_id = ?",
                params,
            )
            db.conn.commit()
            return jsonify({'success': True, 'updated': cur.rowcount})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/screeners/saved/<int:sid>', methods=['DELETE'])
    @require_auth
    def delete_saved(sid):
        db = get_db()
        ensure_schema(db)
        try:
            cur = db.conn.cursor()
            cur.execute(
                "DELETE FROM saved_screeners WHERE id = ? AND user_id = ?",
                (sid, str(g.user_id)),
            )
            db.conn.commit()
            return jsonify({'success': True, 'deleted': cur.rowcount})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    logger.info("saved_screeners: registered /api/screeners/saved (CRUD)")

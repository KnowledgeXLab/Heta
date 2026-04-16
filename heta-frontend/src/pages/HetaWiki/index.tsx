import { useState, useEffect, useRef } from 'react';
import { Settings } from 'lucide-react';
import PageShell from '../../components/layout/PageShell';
import Button from '../../components/ui/Button';
import GraphView from './components/GraphView';
import WikiPagePanel from './components/WikiPagePanel';
import IngestSection from './components/IngestSection';
import Pagination from '../Dataset/components/Pagination';
import { API } from '../../api/endpoints';
import styles from './HetaWiki.module.css';

const PAGE_SIZE = 20;

// ── Types ────────────────────────────────────────────────────────────────────

interface LintConfig { enabled: boolean; interval_hours: number; next_run: string | null }
interface PageNode   { id: string; title: string; category?: string }

// ── Helpers ──────────────────────────────────────────────────────────────────

const BASE = 'http://localhost:8000';

function stripDatePrefix(s: string) {
  return s
    .replace(/^\d{4}-\d{2}-\d{2}[_-]\d{6}[_-]/, '') // YYYY-MM-DD_HHMMSS_
    .replace(/^\d{4}-\d{2}-\d{2}[_-]/, '');           // YYYY-MM-DD_
}

// ── Lint settings panel ───────────────────────────────────────────────────────

function LintPanel() {
  const [cfg, setCfg]       = useState<LintConfig | null>(null);
  const [open, setOpen]     = useState(false);
  const [hours, setHours]   = useState('');
  const [saving, setSaving] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch(`${BASE}${API.wiki.lint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }).then(r => r.json()).then(setCfg).catch(() => {});
  }, []);

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  async function save() {
    setSaving(true);
    try {
      const body: Record<string, unknown> = {};
      if (hours !== '') body.interval_hours = parseInt(hours, 10);
      const res = await fetch(`${BASE}${API.wiki.lint}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      setCfg(await res.json());
      setOpen(false);
      setHours('');
    } finally { setSaving(false); }
  }

  async function toggleEnabled() {
    if (!cfg) return;
    const res = await fetch(`${BASE}${API.wiki.lint}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !cfg.enabled }),
    });
    setCfg(await res.json());
  }

  return (
    <div className={styles.lintWrap} ref={ref}>
      <Button variant="secondary" size="sm" onClick={() => setOpen(o => !o)}>
        <Settings size={14} />
        Lint
        {cfg && <span className={cfg.enabled ? styles.dotOn : styles.dotOff} />}
      </Button>

      {open && (
        <div className={styles.lintDropdown}>
          <div className={styles.lintRow}>
            <span className={styles.lintLabel}>Auto lint</span>
            <button
              className={[styles.toggle, cfg?.enabled ? styles.toggleOn : ''].join(' ')}
              onClick={toggleEnabled}
            >
              {cfg?.enabled ? 'enabled' : 'disabled'}
            </button>
          </div>
          <div className={styles.lintRow}>
            <span className={styles.lintLabel}>Interval (hours)</span>
            <input
              className={styles.lintInput}
              type="number" min={1}
              placeholder={String(cfg?.interval_hours ?? 24)}
              value={hours}
              onChange={e => setHours(e.target.value)}
            />
          </div>
          {cfg?.next_run && (
            <p className={styles.lintNext}>
              next run: {new Date(cfg.next_run).toLocaleString()}
            </p>
          )}
          <Button variant="primary" size="sm" loading={saving} onClick={save}>
            save
          </Button>
        </div>
      )}
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function HetaWikiPage() {
  const [pages, setPages]               = useState<PageNode[]>([]);
  const [selectedNode, setSelectedNode] = useState<{ id: string; title: string } | null>(null);
  const [listPage, setListPage]         = useState(1);

  useEffect(() => {
    fetch(API.wiki.graph)
      .then(r => r.json())
      .then((d: { nodes: PageNode[] }) => setPages(d.nodes))
      .catch(() => {});
  }, []);

  function handleNodeSelect(nodeId: string, title: string) {
    setSelectedNode(prev => prev?.id === nodeId ? null : { id: nodeId, title });
  }

  function handlePageChange(next: number) {
    setListPage(next);
    setSelectedNode(null);
  }

  const totalPages  = Math.max(1, Math.ceil(pages.length / PAGE_SIZE));
  const visiblePages = pages.slice((listPage - 1) * PAGE_SIZE, listPage * PAGE_SIZE);

  return (
    <div className={styles.layout}>
      <div className={styles.main}>
        <PageShell title="HetaWiki" actions={<LintPanel />}>

          {/* ── Ingest ── */}
          <section className={styles.ingestSection}>
            <IngestSection />
          </section>

          {/* ── Graph ── */}
          <section className={styles.graphSection}>
            <GraphView
              selectedNodeId={selectedNode?.id}
              onNodeSelect={handleNodeSelect}
            />
          </section>

          {/* ── Page list ── */}
          {pages.length > 0 && (
            <section className={styles.listSection}>
              <h2 className={styles.listHeading}>
                Pages
                <span className={styles.listCount}>{pages.length}</span>
              </h2>
              <div className={styles.pageList}>
                {visiblePages.map(p => {
                  const title = stripDatePrefix(p.title);
                  const isSelected = selectedNode?.id === p.id;
                  return (
                    <button
                      key={p.id}
                      className={[styles.pageRow, isSelected ? styles.pageRowActive : ''].join(' ')}
                      onClick={() => handleNodeSelect(p.id, title)}
                    >
                      <span className={styles.pageTitle}>{title}</span>
                      {p.category && (
                        <span className={styles.categoryBadge}>{p.category}</span>
                      )}
                    </button>
                  );
                })}
              </div>
              <Pagination page={listPage} totalPages={totalPages} onChange={handlePageChange} />
            </section>
          )}

        </PageShell>
      </div>

      {selectedNode && (
        <WikiPagePanel
          nodeId={selectedNode.id}
          title={selectedNode.title}
          onClose={() => setSelectedNode(null)}
        />
      )}
    </div>
  );
}

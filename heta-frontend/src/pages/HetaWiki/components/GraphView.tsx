import { useEffect, useRef, useState, useCallback } from 'react';
import { API } from '../../../api/endpoints';
import styles from './GraphView.module.css';

// ── Types ────────────────────────────────────────────────────────────────────

interface RawNode { id: string; title: string; category?: string }
interface RawEdge { source: string; target: string }

interface SimNode extends RawNode {
  x: number; y: number;
  vx: number; vy: number;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Strip leading datetime prefixes like "2024-01-15_133525_" or "2024-01-15_" */
function stripDatePrefix(name: string): string {
  return name
    .replace(/^\d{4}-\d{2}-\d{2}[_-]\d{6}[_-]/, '') // YYYY-MM-DD_HHMMSS_
    .replace(/^\d{4}-\d{2}-\d{2}[_-]/, '');           // YYYY-MM-DD_
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

// ── Force simulation (runs outside React state for perf) ─────────────────────

const REPEL   = 4500;
const ATTRACT = 0.08;
const GRAVITY = 0.04;
const DAMPING = 0.82;
const MIN_V   = 0.01;

function tick(nodes: SimNode[], edges: RawEdge[], W: number, H: number) {
  const cx = W / 2, cy = H / 2;

  // Repulsion between all pairs
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const dx = nodes[i].x - nodes[j].x;
      const dy = nodes[i].y - nodes[j].y;
      const dist2 = dx * dx + dy * dy + 0.01;
      const force = REPEL / dist2;
      const fx = (dx / Math.sqrt(dist2)) * force;
      const fy = (dy / Math.sqrt(dist2)) * force;
      nodes[i].vx += fx; nodes[i].vy += fy;
      nodes[j].vx -= fx; nodes[j].vy -= fy;
    }
  }

  // Edge attraction
  const nodeById = Object.fromEntries(nodes.map(n => [n.id, n]));
  for (const e of edges) {
    const s = nodeById[e.source], t = nodeById[e.target];
    if (!s || !t) continue;
    const dx = t.x - s.x, dy = t.y - s.y;
    s.vx += dx * ATTRACT; s.vy += dy * ATTRACT;
    t.vx -= dx * ATTRACT; t.vy -= dy * ATTRACT;
  }

  // Gravity towards center + integrate
  for (const n of nodes) {
    n.vx += (cx - n.x) * GRAVITY;
    n.vy += (cy - n.y) * GRAVITY;
    n.vx *= DAMPING; n.vy *= DAMPING;
    n.x += n.vx;     n.y += n.vy;
    // Clamp inside canvas with padding
    n.x = Math.max(30, Math.min(W - 30, n.x));
    n.y = Math.max(30, Math.min(H - 30, n.y));
  }
}

function isSettled(nodes: SimNode[]) {
  return nodes.every(n => Math.abs(n.vx) < MIN_V && Math.abs(n.vy) < MIN_V);
}

// ── Component ────────────────────────────────────────────────────────────────

const W = 720, H = 480;

interface Props {
  selectedNodeId?: string | null;
  onNodeSelect: (nodeId: string, title: string) => void;
}

export default function GraphView({ selectedNodeId, onNodeSelect }: Props) {
  const [rawNodes, setRawNodes] = useState<RawNode[]>([]);
  const [edges, setEdges]       = useState<RawEdge[]>([]);
  const [loading, setLoading]   = useState(false);
  const [error, setError]       = useState('');

  const simRef = useRef<SimNode[]>([]);
  const rafRef = useRef<number>(0);
  const [frame, setFrame] = useState(0);

  const load = useCallback(async () => {
    setLoading(true); setError('');
    cancelAnimationFrame(rafRef.current);
    try {
      const res  = await fetch(API.wiki.graph);
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json() as { nodes: RawNode[]; edges: RawEdge[] };
      setRawNodes(data.nodes);
      setEdges(data.edges);

      const r = Math.min(160, 40 + data.nodes.length * 14);
      simRef.current = data.nodes.map((n, i) => ({
        ...n,
        x: W / 2 + r * Math.cos((2 * Math.PI * i) / data.nodes.length) + (Math.random() - 0.5) * 20,
        y: H / 2 + r * Math.sin((2 * Math.PI * i) / data.nodes.length) + (Math.random() - 0.5) * 20,
        vx: 0, vy: 0,
      }));
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (simRef.current.length === 0) return;
    let stopped = false;
    function loop() {
      if (stopped) return;
      tick(simRef.current, edges, W, H);
      setFrame(f => f + 1);
      if (!isSettled(simRef.current)) rafRef.current = requestAnimationFrame(loop);
    }
    rafRef.current = requestAnimationFrame(loop);
    return () => { stopped = true; cancelAnimationFrame(rafRef.current); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rawNodes, edges]);

  useEffect(() => { load(); }, [load]);

  const nodes = simRef.current;
  void frame;

  return (
    <div className={styles.graphArea}>
      {rawNodes.length > 0 && (
        <span className={styles.hint}>{rawNodes.length} pages · {edges.length} links</span>
      )}

      {error && <p className={styles.errorMsg}>{error}</p>}

      {!loading && nodes.length === 0 && !error && (
        <p className={styles.placeholder}>No pages in the wiki yet.</p>
      )}

      {nodes.length > 0 && (
        <svg className={styles.svg} viewBox={`0 0 ${W} ${H}`}>
          <defs>
            <marker
              id="wiki-arrow"
              viewBox="0 0 6 6"
              refX={5} refY={3}
              markerWidth={6} markerHeight={6}
              orient="auto-start-reverse"
            >
              <path d="M0,0 L6,3 L0,6 z" className={styles.arrowHead} />
            </marker>
          </defs>

          {edges.map((e, i) => {
            const s = nodes.find(n => n.id === e.source);
            const t = nodes.find(n => n.id === e.target);
            if (!s || !t) return null;
            const dx = t.x - s.x, dy = t.y - s.y;
            const len = Math.sqrt(dx * dx + dy * dy) || 1;
            const nr = 22;
            return (
              <line
                key={i}
                x1={s.x + (dx / len) * nr}  y1={s.y + (dy / len) * nr}
                x2={t.x - (dx / len) * nr}  y2={t.y - (dy / len) * nr}
                className={styles.edge}
                markerEnd="url(#wiki-arrow)"
              />
            );
          })}

          {nodes.map(n => {
            const isSelected = n.id === selectedNodeId;
            const isSynthesis = n.category === 'synthesis';
            const circleClass = [
              styles.nodeCircle,
              isSelected  ? styles.nodeSelected  : '',
              isSynthesis ? styles.nodeSynthesis : '',
            ].filter(Boolean).join(' ');
            const displayTitle = stripDatePrefix(n.title);

            return (
              <g
                key={n.id}
                className={styles.nodeGroup}
                onClick={() => onNodeSelect(n.id, stripDatePrefix(n.title))}
              >
                <circle cx={n.x} cy={n.y} r={22} className={circleClass} />
                <text
                  x={n.x} y={n.y + 1}
                  className={styles.nodeLabel}
                  textAnchor="middle"
                  dominantBaseline="middle"
                >
                  {truncate(displayTitle, 9)}
                </text>
                <text
                  x={n.x} y={n.y + 36}
                  className={styles.nodeSub}
                  textAnchor="middle"
                >
                  {truncate(displayTitle, 16)}
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}

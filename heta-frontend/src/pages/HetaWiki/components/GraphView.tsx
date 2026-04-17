import { useEffect, useRef, useState, useCallback } from 'react';
import {
  forceSimulation,
  forceManyBody,
  forceLink,
  forceCenter,
  forceCollide,
  type Simulation,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force';
import { API } from '../../../api/endpoints';
import styles from './GraphView.module.css';

// ── Types ────────────────────────────────────────────────────────────────────

interface RawNode { id: string; title: string; category?: string }
interface RawEdge { source: string; target: string }

// D3 mutates nodes in-place, extending them with x/y/vx/vy
interface SimNode extends RawNode, SimulationNodeDatum {
  id: string;
  x: number;
  y: number;
}
type SimLink = SimulationLinkDatum<SimNode> & { _source: string; _target: string }

// ── Helpers ──────────────────────────────────────────────────────────────────

function stripDatePrefix(name: string): string {
  return name
    .replace(/^\d{4}-\d{2}-\d{2}[_-]\d{6}[_-]/, '')
    .replace(/^\d{4}-\d{2}-\d{2}[_-]/, '');
}

function truncate(s: string, n: number) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

/** Quadratic bezier path between two nodes, shortened to circle edge */
function curvedPath(sx: number, sy: number, tx: number, ty: number, r: number): string {
  const dx = tx - sx, dy = ty - sy;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  const x1 = sx + (dx / len) * r,  y1 = sy + (dy / len) * r;
  const x2 = tx - (dx / len) * r,  y2 = ty - (dy / len) * r;
  // Control point: perp offset from midpoint, 15% of edge length
  const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
  const offset = len * 0.15;
  const cx = mx - (dy / len) * offset, cy = my + (dx / len) * offset;
  return `M ${x1} ${y1} Q ${cx} ${cy} ${x2} ${y2}`;
}

// ── Component ────────────────────────────────────────────────────────────────

const W = 720, H = 480;

interface Props {
  selectedNodeId?: string | null;
  onNodeSelect: (nodeId: string, title: string) => void;
}

export default function GraphView({ selectedNodeId, onNodeSelect }: Props) {
  const [rawNodes, setRawNodes] = useState<RawNode[]>([]);
  const [rawEdges, setRawEdges] = useState<RawEdge[]>([]);
  const [loading, setLoading]   = useState(false);
  const [error, setError]       = useState('');
  const [, setTick]             = useState(0);   // triggers re-render on each tick

  // D3 simulation lives outside React state
  const simRef   = useRef<Simulation<SimNode, SimLink> | null>(null);
  const nodesRef = useRef<SimNode[]>([]);
  const linksRef = useRef<SimLink[]>([]);

  const load = useCallback(async () => {
    setLoading(true); setError('');

    // Stop any existing simulation
    simRef.current?.stop();

    try {
      const res  = await fetch(API.wiki.graph);
      if (!res.ok) throw new Error(`${res.status}`);
      const data = await res.json() as { nodes: RawNode[]; edges: RawEdge[] };

      setRawNodes(data.nodes);
      setRawEdges(data.edges);

      // Seed positions on a circle so nodes don't start all at origin
      const r = Math.min(160, 40 + data.nodes.length * 14);
      nodesRef.current = data.nodes.map((n, i) => ({
        ...n,
        x: W / 2 + r * Math.cos((2 * Math.PI * i) / data.nodes.length),
        y: H / 2 + r * Math.sin((2 * Math.PI * i) / data.nodes.length),
      }));

      // Build link objects referencing the node objects
      const nodeById = Object.fromEntries(nodesRef.current.map(n => [n.id, n]));
      linksRef.current = data.edges
        .filter(e => nodeById[e.source] && nodeById[e.target])
        .map(e => ({
          source: nodeById[e.source],
          target: nodeById[e.target],
          _source: e.source,
          _target: e.target,
        }));

      // Create D3 simulation
      simRef.current = forceSimulation<SimNode, SimLink>(nodesRef.current)
        .force('charge', forceManyBody<SimNode>().strength(-300))
        .force('link',   forceLink<SimNode, SimLink>(linksRef.current).distance(120).strength(0.5))
        .force('center', forceCenter<SimNode>(W / 2, H / 2).strength(0.1))
        .force('collide', forceCollide<SimNode>(30))
        .alphaDecay(0.03)      // slower cooling → smoother settling
        .velocityDecay(0.4)    // D3 default — good friction
        .on('tick', () => {
          // Clamp nodes inside canvas
          for (const n of nodesRef.current) {
            n.x = Math.max(30, Math.min(W - 30, n.x ?? W / 2));
            n.y = Math.max(40, Math.min(H - 50, n.y ?? H / 2));
          }
          setTick(t => t + 1);
        });

    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    return () => { simRef.current?.stop(); };
  }, [load]);

  const nodes = nodesRef.current;
  const links = linksRef.current;

  return (
    <div className={styles.graphArea}>
      {rawNodes.length > 0 && (
        <span className={styles.hint}>{rawNodes.length} pages · {rawEdges.length} links</span>
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

          {links.map((lk, i) => {
            const s = lk.source as SimNode;
            const t = lk.target as SimNode;
            if (s.x == null || t.x == null) return null;
            return (
              <path
                key={i}
                d={curvedPath(s.x, s.y, t.x, t.y, 23)}
                className={styles.edge}
                markerEnd="url(#wiki-arrow)"
              />
            );
          })}

          {nodes.map(n => {
            if (n.x == null) return null;
            const isSelected  = n.id === selectedNodeId;
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
                  x={n.x} y={n.y + 36}
                  className={styles.nodeLabel}
                  textAnchor="middle"
                >
                  {truncate(displayTitle, 14)}
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}

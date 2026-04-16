import { useEffect, useState, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import { X, Copy, Check } from 'lucide-react';
import { API } from '../../../api/endpoints';
import styles from './WikiPagePanel.module.css';

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Strip YAML frontmatter so it doesn't render as raw text */
function stripFrontmatter(text: string): string {
  if (!text.startsWith('---')) return text;
  const end = text.indexOf('\n---', 3);
  if (end === -1) return text;
  return text.slice(end + 4).trimStart();
}

/** Extract a single frontmatter field */
function getFrontmatterField(text: string, field: string): string | null {
  if (!text.startsWith('---')) return null;
  const end = text.indexOf('\n---', 3);
  if (end === -1) return null;
  const block = text.slice(3, end);
  const match = block.match(new RegExp(`^${field}:\\s*(.+)$`, 'm'));
  return match ? match[1].trim().replace(/^["']|["']$/g, '') : null;
}

// ── Skeleton loader ───────────────────────────────────────────────────────────

function Skeleton() {
  return (
    <div className={styles.skeleton}>
      <div className={[styles.skLine, styles.skH1].join(' ')} />
      <div className={[styles.skLine, styles.skP].join(' ')} />
      <div className={[styles.skLine, styles.skP, styles.skShort].join(' ')} />
      <div className={[styles.skLine, styles.skH2].join(' ')} style={{ marginTop: 28 }} />
      <div className={[styles.skLine, styles.skP].join(' ')} />
      <div className={[styles.skLine, styles.skP].join(' ')} />
      <div className={[styles.skLine, styles.skP, styles.skShort].join(' ')} />
      <div className={[styles.skLine, styles.skCode].join(' ')} style={{ marginTop: 20 }} />
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

interface Props {
  nodeId: string;
  title: string;
  onClose: () => void;
}

export default function WikiPagePanel({ nodeId, title, onClose }: Props) {
  const [raw, setRaw]         = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState('');
  const [copied, setCopied]   = useState(false);

  const stem = nodeId.replace(/^pages\//, '');

  useEffect(() => {
    setLoading(true); setError(''); setRaw('');
    fetch(API.wiki.page(stem))
      .then(r => { if (!r.ok) throw new Error(`${r.status} ${r.statusText}`); return r.json(); })
      .then((d: { content: string }) => setRaw(d.content))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [stem]);

  const handleCopy = useCallback(() => {
    const body = stripFrontmatter(raw);
    navigator.clipboard.writeText(body).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [raw]);

  const category = raw ? getFrontmatterField(raw, 'category') : null;
  const body     = raw ? stripFrontmatter(raw) : '';

  return (
    <div className={styles.panel}>
      {/* ── Header ── */}
      <div className={styles.header}>
        <div className={styles.headerLeft}>
          <span className={styles.title}>{title}</span>
          {category && <span className={styles.categoryBadge}>{category}</span>}
        </div>
        <div className={styles.headerActions}>
          <button
            className={styles.iconBtn}
            onClick={handleCopy}
            disabled={loading || !!error}
            title="Copy content"
          >
            {copied ? <Check size={14} strokeWidth={2} /> : <Copy size={14} strokeWidth={1.75} />}
          </button>
          <button className={styles.iconBtn} onClick={onClose} title="Close">
            <X size={15} strokeWidth={1.75} />
          </button>
        </div>
      </div>

      {/* ── Body ── */}
      <div className={styles.body}>
        {loading && <Skeleton />}
        {error   && <p className={styles.errorMsg}>{error}</p>}
        {!loading && !error && (
          <div className="prose">
            <ReactMarkdown rehypePlugins={[rehypeHighlight]}>
              {body}
            </ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}

import { useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import { BookMarked } from 'lucide-react';
import ChatInput from '../Chat/components/ChatInput';
import Spinner from '../Chat/components/Spinner';
import WikiPagePanel from '../HetaWiki/components/WikiPagePanel';
import { API } from '../../api/endpoints';
import styles from './HetaWikiQuery.module.css';

// ── Types ─────────────────────────────────────────────────────────────────────

interface TaskPoll { status: string; message?: string; error?: string; result?: Record<string, unknown> }
interface QueryResult { answer: string; sources: string[]; archived: string | null }

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  sources?: string[];
  archived?: string | null;
  error?: boolean;
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface PageNode { id: string; title: string }

// ── Helpers ───────────────────────────────────────────────────────────────────

const BASE = 'http://localhost:8000';

function pollTask(
  taskId: string,
  onDone: (r: TaskPoll) => void,
  onFail: (e: string) => void,
) {
  const iv = setInterval(async () => {
    try {
      const res  = await fetch(`${BASE}${API.wiki.task(taskId)}`);
      const data = (await res.json()) as TaskPoll;
      if (data.status === 'completed') { clearInterval(iv); onDone(data); }
      if (data.status === 'failed' || data.status === 'cancelled') {
        clearInterval(iv); onFail(data.error ?? 'unknown error');
      }
    } catch { clearInterval(iv); onFail('network error'); }
  }, 1500);
}

// ── Message bubbles ───────────────────────────────────────────────────────────

function UserBubble({ content }: { content: string }) {
  return (
    <div className={styles.userRow}>
      <div className={styles.userBubble}>{content}</div>
    </div>
  );
}

function AssistantBubble({ content, sources, archived, error, nodeMap, onSourceClick }: {
  content: string;
  sources?: string[];
  archived?: string | null;
  error?: boolean;
  nodeMap: Record<string, string>;   // title.toLowerCase() → nodeId
  onSourceClick: (nodeId: string, title: string) => void;
}) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const hasSources = sources && sources.length > 0;

  return (
    <div className={styles.assistantRow}>
      <div className={styles.assistantContent}>
        <div className={[styles.assistantBubble, error ? styles.errorBubble : ''].join(' ')}>
          <div className={styles.markdown}>
            <ReactMarkdown rehypePlugins={[rehypeHighlight]}>{content}</ReactMarkdown>
          </div>
        </div>

        {(hasSources || archived) && (
          <div className={styles.meta}>
            {hasSources && (
              <div className={styles.sources}>
                <button
                  className={styles.sourcesToggle}
                  onClick={() => setSourcesOpen(o => !o)}
                >
                  {sourcesOpen ? '▾' : '▸'} {sources!.length} wiki page{sources!.length !== 1 ? 's' : ''}
                </button>
                {sourcesOpen && (
                  <div className={styles.sourceList}>
                    {sources!.map(s => {
                      const nodeId = nodeMap[s.toLowerCase()];
                      return (
                        <button
                          key={s}
                          className={[styles.sourceChip, nodeId ? styles.sourceChipClickable : ''].join(' ')}
                          onClick={() => nodeId && onSourceClick(nodeId, s)}
                          disabled={!nodeId}
                        >
                          <BookMarked size={11} className={styles.sourceIcon} />
                          {s}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
            {archived && (
              <span className={styles.archivedBadge}>
                archived → {archived}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function HetaWikiQueryPage() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput]       = useState('');
  const [loading, setLoading]   = useState(false);
  const [nodeMap, setNodeMap]   = useState<Record<string, string>>({});
  const [panel, setPanel]       = useState<{ id: string; title: string } | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Build title→nodeId map from graph for source chip navigation
  useEffect(() => {
    fetch(API.wiki.graph)
      .then(r => r.json())
      .then((d: { nodes: PageNode[] }) => {
        const map: Record<string, string> = {};
        d.nodes.forEach(n => { map[n.title.toLowerCase()] = n.id; });
        setNodeMap(map);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  async function handleSubmit() {
    const question = input.trim();
    if (!question || loading) return;

    setMessages(prev => [...prev, { id: crypto.randomUUID(), role: 'user', content: question }]);
    setInput('');
    setLoading(true);

    try {
      const res  = await fetch(`${BASE}${API.wiki.query}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question }),
      });
      const data = await res.json();

      pollTask(
        data.task_id,
        (r) => {
          const result = r.result as unknown as QueryResult;
          setMessages(prev => [...prev, {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: result.answer,
            sources: result.sources,
            archived: result.archived,
          }]);
          setLoading(false);
        },
        (e) => {
          setMessages(prev => [...prev, {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: e,
            error: true,
          }]);
          setLoading(false);
        },
      );
    } catch (e) {
      setMessages(prev => [...prev, {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: String(e),
        error: true,
      }]);
      setLoading(false);
    }
  }

  const isEmpty = messages.length === 0 && !loading;

  return (
    <div className={styles.layout}>
      <div className={styles.page}>
        <div className={styles.topbar}>
          <h1 className={styles.title}>Query</h1>
        </div>

        <div className={styles.messageList}>
          {isEmpty && (
            <div className={styles.empty}>
              <p className={styles.emptyTitle}>Ask anything about the wiki</p>
              <p className={styles.emptyHint}>The agent reads relevant pages and synthesises an answer.</p>
            </div>
          )}
          <div className={styles.messages}>
            {messages.map(msg =>
              msg.role === 'user'
                ? <UserBubble key={msg.id} content={msg.content} />
                : <AssistantBubble
                    key={msg.id}
                    content={msg.content}
                    sources={msg.sources}
                    archived={msg.archived}
                    error={msg.error}
                    nodeMap={nodeMap}
                    onSourceClick={(id, title) => setPanel(p => p?.id === id ? null : { id, title })}
                  />
            )}
            {loading && <div className={styles.spinnerRow}><Spinner /></div>}
            <div ref={bottomRef} />
          </div>
        </div>

        <ChatInput
          value={input}
          onChange={setInput}
          onSubmit={handleSubmit}
          disabled={loading}
          placeholder="Ask a question about the wiki… (Enter to send)"
        />
      </div>

      {panel && (
        <WikiPagePanel
          nodeId={panel.id}
          title={panel.title}
          onClose={() => setPanel(null)}
        />
      )}
    </div>
  );
}

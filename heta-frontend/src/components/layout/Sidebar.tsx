import { NavLink } from 'react-router-dom';
import { Database, LibraryBig, MessageSquare, BrainCircuit, Sparkles, ListTodo, BookMarked, Search, type LucideIcon } from 'lucide-react';
import styles from './Sidebar.module.css';

interface NavItem {
  to: string;
  label: string;
  Icon: LucideIcon;
}

interface Section {
  label: string;
  items: NavItem[];
}

const SECTIONS: Section[] = [
  {
    label: 'HetaDB',
    items: [
      { to: '/datasets', label: 'Datasets',        Icon: Database      },
      { to: '/kb',       label: 'Knowledge Bases', Icon: LibraryBig    },
      { to: '/chat',     label: 'Chat',            Icon: MessageSquare },
      { to: '/tasks',    label: 'Tasks',           Icon: ListTodo      },
    ],
  },
  {
    label: 'HetaMem',
    items: [
      { to: '/hetamem', label: 'Memory & MCP', Icon: BrainCircuit },
    ],
  },
  {
    label: 'HetaGen',
    items: [
      { to: '/hetagen', label: 'HetaGen', Icon: Sparkles },
    ],
  },
  {
    label: 'HetaWiki',
    items: [
      { to: '/hetawiki',       label: 'Wiki',  Icon: BookMarked },
      { to: '/hetawiki/query', label: 'Query', Icon: Search     },
    ],
  },
];

export default function Sidebar() {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <span className={styles.brandName}>Heta</span>
      </div>

      <nav className={styles.nav}>
        {SECTIONS.map((section) => (
          <div key={section.label} className={styles.section}>
            <span className={styles.sectionLabel}>{section.label}</span>
            {section.items.map(({ to, label, Icon }) => (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) =>
                  [styles.link, isActive ? styles.active : ''].join(' ')
                }
              >
                <Icon className={styles.icon} size={16} strokeWidth={1.75} />
                {label}
              </NavLink>
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}

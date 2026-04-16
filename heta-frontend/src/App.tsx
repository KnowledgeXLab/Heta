import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Sidebar from './components/layout/Sidebar';
import DatasetPage from './pages/Dataset';
import KnowledgeBasePage from './pages/KnowledgeBase';
import ChatPage from './pages/Chat';
import HetaMemPage from './pages/HetaMem';
import HetaGenPage from './pages/HetaGen';
import HetaWikiPage from './pages/HetaWiki';
import HetaWikiQueryPage from './pages/HetaWikiQuery';
import TasksPage from './pages/Tasks';
import styles from './App.module.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 30_000 },
  },
});

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className={styles.root}>
          <Sidebar />
          <Routes>
            <Route path="/" element={<Navigate to="/datasets" replace />} />
            <Route path="/datasets" element={<DatasetPage />} />
            <Route path="/kb" element={<KnowledgeBasePage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/hetamem" element={<HetaMemPage />} />
            <Route path="/hetagen"  element={<HetaGenPage />} />
            <Route path="/hetawiki"        element={<HetaWikiPage />} />
            <Route path="/hetawiki/query"  element={<HetaWikiQueryPage />} />
            <Route path="/tasks"   element={<TasksPage />} />
          </Routes>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

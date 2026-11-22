import {createRoot} from 'react-dom/client';
import App from './App.tsx';
import './index.css';
import { applyTheme, initialTheme } from './services/theme';

applyTheme(initialTheme(), false);

createRoot(document.getElementById('root')!).render(<App />);

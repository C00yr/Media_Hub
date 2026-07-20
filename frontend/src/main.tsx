import React from "react";
import { createRoot } from "react-dom/client";
import type { Root } from "react-dom/client";
import { App } from "./app/App";
import "./styles/global.css";

const rootElement = document.getElementById("root")!;
const runtime = globalThis as typeof globalThis & { __ptMediaHubRoot?: Root };
const root = runtime.__ptMediaHubRoot ?? createRoot(rootElement);

if (import.meta.hot) runtime.__ptMediaHubRoot = root;

root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

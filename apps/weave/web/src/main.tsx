import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "@xyflow/react/dist/style.css";
import "./fonts/misans/MiSans-Regular.css";
import "./fonts/misans/MiSans-Medium.css";
import "./fonts/misans/MiSans-Semibold.css";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("The application root is missing.");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

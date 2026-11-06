import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import { layoutDag, NODE_H, NODE_W } from "../lib/dag";
import type { TaskRun, TaskSpec } from "../lib/types";
import { StatusBadge, statusStroke } from "./StatusBadge";

type TaskNodeData = { spec: TaskSpec; run?: TaskRun; selected: boolean };

function TaskNode({ data }: NodeProps<Node<TaskNodeData>>) {
  const { spec, run, selected } = data;
  const stroke = run ? statusStroke(run.status) : "#2b3850";
  return (
    <div
      className={`rounded-xl border bg-surface-2 px-3 py-2 transition-shadow ${selected ? "shadow-glow" : ""}`}
      style={{ width: NODE_W, height: NODE_H, borderColor: stroke }}
    >
      <Handle type="target" position={Position.Left} className="!border-0 !bg-line" />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[13px] font-medium">{spec.key}</span>
        {run && <StatusBadge status={run.status} compact />}
      </div>
      <div className="mt-1 flex items-center justify-between text-[11px] text-ink-faint">
        <span className="truncate">{spec.operator}</span>
        {run && run.attempt_count > 1 && <span>attempt {run.attempt_count}/{run.max_attempts}</span>}
      </div>
      <Handle type="source" position={Position.Right} className="!border-0 !bg-line" />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

export function DagView({ tasks, runs, selected, onSelect }: {
  tasks: TaskSpec[];
  runs?: TaskRun[];
  selected?: string | null;
  onSelect?: (key: string) => void;
}) {
  const { nodes, edges, height } = useMemo(() => {
    const byKey = new Map(runs?.map((r) => [r.task_key, r]));
    const placed = layoutDag(tasks.map((t) => ({ key: t.key, dependsOn: t.depends_on ?? [] })));
    const nodes: Node<TaskNodeData>[] = placed.map((p) => ({
      id: p.key,
      type: "task",
      position: { x: p.x, y: p.y },
      data: { spec: tasks.find((t) => t.key === p.key)!, run: byKey.get(p.key), selected: selected === p.key },
      draggable: false,
    }));
    const edges: Edge[] = tasks.flatMap((t) =>
      (t.depends_on ?? []).map((parent) => {
        const downstream = byKey.get(t.key);
        return {
          id: `${parent}->${t.key}`,
          source: parent,
          target: t.key,
          animated: downstream?.status === "running" || downstream?.status === "queued",
          style: downstream?.status === "succeeded" ? { stroke: "#3d8bff88" } : undefined,
        };
      }),
    );
    // Size the canvas to the graph: no empty acres around a three-task chain.
    const rows = Math.max(1, ...placed.map((p) => p.row + 1));
    const height = Math.min(440, Math.max(170, rows * (NODE_H + 28) + 90));
    return { nodes, edges, height };
  }, [tasks, runs, selected]);
  // fitView runs on mount, so remount when the *shape* changes (e.g. the definition
  // arrives after the task runs), but not on every status update.
  const shape = tasks.map((t) => `${t.key}<${(t.depends_on ?? []).join(",")}`).join("|");

  return (
    <div className="card overflow-hidden" style={{ height }}>
      <ReactFlow
        key={shape}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
        nodesConnectable={false}
        elementsSelectable={Boolean(onSelect)}
        onNodeClick={(_, node) => onSelect?.(node.id)}
        proOptions={{ hideAttribution: true }}
        minZoom={0.3}
      >
        <Background color="#1a2233" gap={20} />
      </ReactFlow>
    </div>
  );
}

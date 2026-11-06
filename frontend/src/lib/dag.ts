/** Layered DAG layout: column = longest path from a root, so every edge points right.
 *  Small, deterministic and dependency-free. Workflows are tens of tasks, not thousands. */

export interface DagNode { key: string; dependsOn: string[] }
export interface Placed { key: string; column: number; row: number; x: number; y: number }

export const NODE_W = 200;
export const NODE_H = 64;
const GAP_X = 80;
const GAP_Y = 28;

export function layoutDag(nodes: DagNode[]): Placed[] {
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const column = new Map<string, number>();
  const visiting = new Set<string>();

  const depth = (key: string): number => {
    const known = column.get(key);
    if (known !== undefined) return known;
    if (visiting.has(key)) throw new Error(`cycle at ${key}`); // the API rejects these anyway
    visiting.add(key);
    const parents = byKey.get(key)?.dependsOn.filter((p) => byKey.has(p)) ?? [];
    const value = parents.length ? Math.max(...parents.map(depth)) + 1 : 0;
    visiting.delete(key);
    column.set(key, value);
    return value;
  };
  nodes.forEach((n) => depth(n.key));

  const columns = new Map<number, string[]>();
  for (const n of nodes) {
    const c = column.get(n.key)!;
    columns.set(c, [...(columns.get(c) ?? []), n.key]);
  }
  const tallest = Math.max(0, ...[...columns.values()].map((keys) => keys.length));
  const placed: Placed[] = [];
  for (const [c, keys] of columns) {
    const offset = ((tallest - keys.length) * (NODE_H + GAP_Y)) / 2; // centre each column
    keys.forEach((key, row) =>
      placed.push({ key, column: c, row, x: c * (NODE_W + GAP_X), y: offset + row * (NODE_H + GAP_Y) }),
    );
  }
  return placed.sort((a, b) => a.column - b.column || a.row - b.row);
}

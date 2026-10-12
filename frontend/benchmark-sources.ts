export const BENCHMARK_SOURCES = {
  artificial_analysis: { name: "Artificial Analysis", shortName: "AA" },
  bfcl: { name: "BFCL", shortName: "BFCL" },
  superclue: { name: "SuperCLUE", shortName: "SuperCLUE" },
} as const;

export type BenchmarkSource = keyof typeof BENCHMARK_SOURCES;
export const DEFAULT_BENCHMARK_SOURCE: BenchmarkSource = "artificial_analysis";

export function resultsForSource<T extends { source: string }>(results: T[] | undefined, source: BenchmarkSource = DEFAULT_BENCHMARK_SOURCE): T[] {
  return (results ?? []).filter(result => result.source === source);
}

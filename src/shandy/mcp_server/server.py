"""
MCP server implementation for SHANDY tools.

Implements Model Context Protocol to provide tools to Claude.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict

# Import tool implementations
from ..code_executor import execute_code, format_execution_result
from ..literature import search_pubmed
from ..knowledge_graph import KnowledgeGraph

# Import pandas for data loading
import pandas as pd


class ShandyMCPServer:
    """MCP server for SHANDY discovery tools."""

    def __init__(self, job_dir: Path, data_file: Path):
        """
        Initialize MCP server.

        Args:
            job_dir: Job directory containing knowledge_graph.json
            data_file: Path to data CSV file
        """
        self.job_dir = Path(job_dir)
        self.data_file = Path(data_file)

        # Load data
        self.data = pd.read_csv(self.data_file)

        # Load or create knowledge graph
        kg_path = self.job_dir / "knowledge_graph.json"
        if kg_path.exists():
            self.kg = KnowledgeGraph.load(kg_path)
        else:
            # This shouldn't happen - KG should be created by orchestrator
            raise FileNotFoundError(f"Knowledge graph not found at {kg_path}")

    async def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle incoming MCP request.

        Args:
            request: JSON-RPC request

        Returns:
            JSON-RPC response
        """
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")

        try:
            if method == "tools/list":
                result = self.list_tools()
            elif method == "tools/call":
                tool_name = params.get("name")
                tool_args = params.get("arguments", {})
                result = await self.call_tool(tool_name, tool_args)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"}
                }

            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result
            }

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": str(e)}
            }

    def list_tools(self) -> Dict[str, Any]:
        """List available tools."""
        return {
            "tools": [
                {
                    "name": "execute_code",
                    "description": "Execute Python code to analyze data. Data is available as 'data' DataFrame.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "description": "Python code to execute"
                            }
                        },
                        "required": ["code"]
                    }
                },
                {
                    "name": "search_pubmed",
                    "description": "Search PubMed for scientific papers",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query (e.g., 'hypothermia neuroprotection metabolomics')"
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum number of results to return (default: 10)"
                            }
                        },
                        "required": ["query"]
                    }
                },
                {
                    "name": "update_knowledge_graph",
                    "description": "Record a confirmed finding to the knowledge graph",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Finding title"
                            },
                            "evidence": {
                                "type": "string",
                                "description": "Statistical evidence (p-values, effect sizes, etc.)"
                            },
                            "interpretation": {
                                "type": "string",
                                "description": "Biological/mechanistic interpretation"
                            }
                        },
                        "required": ["title", "evidence"]
                    }
                }
            ]
        }

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool.

        Args:
            tool_name: Name of tool to call
            arguments: Tool arguments

        Returns:
            Tool result
        """
        if tool_name == "execute_code":
            return await self.tool_execute_code(arguments)
        elif tool_name == "search_pubmed":
            return await self.tool_search_pubmed(arguments)
        elif tool_name == "update_knowledge_graph":
            return await self.tool_update_kg(arguments)
        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def tool_execute_code(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute Python code."""
        code = args["code"]

        # Create plots directory
        plots_dir = self.job_dir / "plots"
        plots_dir.mkdir(parents=True, exist_ok=True)

        # Execute code
        result = execute_code(code, self.data, plots_dir, timeout=60)

        # Log to knowledge graph
        self.kg.log_analysis(
            action="execute_code",
            code=code,
            output=result.get("output", ""),
            success=result["success"],
            execution_time=result["execution_time"],
            plots=result.get("plots", [])
        )
        self.kg.save(self.job_dir / "knowledge_graph.json")

        # Format result for Claude
        formatted = format_execution_result(result)

        return {
            "content": [
                {
                    "type": "text",
                    "text": formatted
                }
            ]
        }

    async def tool_search_pubmed(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Search PubMed."""
        query = args["query"]
        max_results = args.get("max_results", 10)

        # Search PubMed
        papers = search_pubmed(query, max_results=max_results)

        # Log to knowledge graph
        for paper in papers:
            self.kg.add_literature(
                pmid=paper["pmid"],
                title=paper["title"],
                abstract=paper["abstract"],
                search_query=query
            )

        self.kg.log_analysis(
            action="search_pubmed",
            query=query,
            results_count=len(papers)
        )
        self.kg.save(self.job_dir / "knowledge_graph.json")

        # Format results
        if not papers:
            result_text = f"No papers found for query: '{query}'"
        else:
            result_parts = [f"Found {len(papers)} papers for query: '{query}'\n"]
            for i, paper in enumerate(papers, 1):
                result_parts.append(
                    f"\n{i}. **{paper['title']}** (PMID: {paper['pmid']}, {paper.get('year', 'N/A')})\n"
                    f"   Authors: {paper.get('authors', 'Unknown')}\n"
                    f"   Abstract: {paper['abstract'][:300]}...\n"
                )
            result_text = "".join(result_parts)

        return {
            "content": [
                {
                    "type": "text",
                    "text": result_text
                }
            ]
        }

    async def tool_update_kg(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """Update knowledge graph with a finding."""
        title = args["title"]
        evidence = args["evidence"]
        interpretation = args.get("interpretation", "")

        # Add finding
        finding_id = self.kg.add_finding(
            title=title,
            evidence=evidence
        )

        # Update interpretation if provided
        if interpretation:
            for finding in self.kg.data["findings"]:
                if finding["id"] == finding_id:
                    finding["biological_interpretation"] = interpretation

        self.kg.save(self.job_dir / "knowledge_graph.json")

        return {
            "content": [
                {
                    "type": "text",
                    "text": f"✅ Finding recorded as {finding_id}: {title}"
                }
            ]
        }

    async def run(self):
        """Run the MCP server (stdio-based)."""
        while True:
            try:
                # Read request from stdin
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline
                )

                if not line:
                    break

                request = json.loads(line)

                # Handle request
                response = await self.handle_request(request)

                # Write response to stdout
                print(json.dumps(response), flush=True)

            except Exception as e:
                # Log error but continue
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": f"Parse error: {str(e)}"}
                }
                print(json.dumps(error_response), flush=True)


def main():
    """Main entry point for MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY MCP Server")
    parser.add_argument("--job-dir", required=True, help="Job directory")
    parser.add_argument("--data-file", required=True, help="Data CSV file")

    args = parser.parse_args()

    # Create and run server
    server = ShandyMCPServer(
        job_dir=args.job_dir,
        data_file=args.data_file
    )

    asyncio.run(server.run())


if __name__ == "__main__":
    main()

class Cairn < Formula
  desc "Local-first shared memory for CLI agents"
  homepage "https://cairncli.com"
  # Untagged HEAD install until releases/tags exist; then switch to a
  # versioned tarball with sha256. No sha256 stanza: not used for git urls.
  url "https://github.com/moonbase2090/cairn.git", branch: "main"
  version "0.3.2"
  license "MPL-2.0"

  depends_on "uv" => :build
  depends_on "python@3.12"

  def install
    # Stage the uv tool install inside the Cellar instead of ~/.local.
    ENV["UV_TOOL_DIR"] = libexec/"tools"
    ENV["UV_TOOL_BIN_DIR"] = bin
    # NOTE: resolves dependencies from PyPI at install time, so this
    # formula needs network during install. Fine for a personal tap;
    # Homebrew core would require vendored resources instead.
    system "uv", "tool", "install", "--force", "."
  end

  def caveats
    <<~EOS
      cairn is installed. Per project, run:
        cairn init --yes && cairn bootstrap
      GUI editors may not inherit your shell PATH — if the MCP server
      fails to start, use the absolute binary path in .mcp.json "command":
        #{opt_bin}/cairn-mcp
    EOS
  end

  test do
    system bin/"cairn", "init", "--help"
  end
end

# Keep Agent knowledge local, versioned, and separate from live facts

Stable explanatory knowledge is stored as versioned Markdown and retrieved through a read-only local tool. Mandatory safety and domain invariants remain in code and tool contracts, while current M-Team, TMDB, and qB facts always come from live tools. This keeps the initial deployment inspectable and dependency-light while leaving room for semantic retrieval if the corpus grows.

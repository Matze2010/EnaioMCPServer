# Start from a base Python image
FROM python:3.10-slim

# For debugging purposes, install curl
RUN apt update && apt install -y \
    curl git \
    && rm -rf /var/lib/apt/lists/*
    
# Set the working directory
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Get the latest version of EnaioMCP from github
RUN git clone https://github.com/Matze2010/EnaioMCPServer.git server

# Expose the port that the MCP server will run on
EXPOSE 8000

# Command to run the server using the refactored entry point
CMD ["fastmcp", "run", "server/EnaioMCP.py", "--host", "0.0.0.0", "--transport", "http", "--log-level", "DEBUG"]

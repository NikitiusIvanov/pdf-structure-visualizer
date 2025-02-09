FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . /app

# Expose the port used by the web server
EXPOSE 8080

# Set environment variables (if needed)
ENV PYTHONUNBUFFERED=1

# Command to run the application using gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "visualization:server"]
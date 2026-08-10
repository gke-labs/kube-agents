"""Combined FastAPI and Streamlit process entry point."""

from admin_console.api.app import create_app
from admin_console.api.streamlit_proxy import portal_lifespan, register_streamlit_proxy

app = create_app(lifespan=portal_lifespan)
register_streamlit_proxy(app)

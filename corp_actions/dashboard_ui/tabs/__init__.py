"""Dashboard tabs - one module per tab, each exposing render().

Each render() is called inside the tab's `with tab_x:` context manager, so
the plain st.* calls inside the function body keep routing to that tab.
"""

FROM nginx:alpine
COPY index.html /usr/share/nginx/html/
COPY failed.html /usr/share/nginx/html/
COPY long-tracks.html /usr/share/nginx/html/
COPY mismatched-tracks.html /usr/share/nginx/html/
COPY login.html /usr/share/nginx/html/
COPY auth.js /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080

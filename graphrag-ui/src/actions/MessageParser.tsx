import React from "react";

interface MessageParserProps {
  children: any;
  actions: any;
}

const MessageParser: React.FC<MessageParserProps> = ({ children, actions }) => {
  const parse = (message: string) => {
    // Ignore empty / whitespace-only submits. Clicking the Stop button just as
    // an answer finishes can leak a send after the input was cleared, which
    // otherwise fires an empty query at the backend.
    if (!message || !message.trim()) return;
    actions.queryGraphragWs(message);
  };

  return (
    <div>
      {React.Children.map(children, (child) => {
        return React.cloneElement(child, {
          parse: parse,
          actions,
        });
      })}
    </div>
  );
};

export default MessageParser;
